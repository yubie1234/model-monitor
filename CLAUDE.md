# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`model-monitor` is a **FastAPI service** for a LiteLLM → KServe → vLLM/SGLang stack. It answers two questions: **which models are actually serving**, and **how many backend Pods (and GPUs) sit behind each `api_base` (load balancer)**. It exposes a web dashboard (`/`), a JSON API (`/api/snapshot`), and Prometheus metrics (`/metrics`).

It used to be a single stdlib-only `model_monitor.py` (TUI/`--json`/`--serve`). It was migrated to FastAPI with a package layout; the TUI and CLI rendering modes were dropped — **web API only**.

**Dependency policy:** the *web layer* uses FastAPI (`fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`). But the *collection layer* (`app/core`, `app/services`) deliberately stays on the **Python standard library** (`urllib`, `ssl`, `json`, `asyncio`, `hashlib`, `hmac`) — no `requests`/`httpx`/k8s-client. Keep it that way: collectors must remain importable and testable without the web stack. PyYAML is optional (config loading degrades to JSON without it). **If a task needs a new external package, ask the user first** — the deployment target is air-gapped and every dependency must be vendored into the image.

## Layout

```
app/
  __init__.py          # __version__ — single source of truth
  main.py              # create_app(), lifespan → starts/stops Refresher; wires app.state
  __main__.py          # `python -m app` launcher (uvicorn.run)
  config.py            # Settings (pydantic-settings) + config-file merge → collector dict
  auth.py              # admin-key check (X-LiteLLM-Key) for locked global endpoints
  core/
    http.py            # http_get_json (urllib, stdlib)
    k8s.py             # K8sClient (in-cluster, urllib+ssl)
  services/
    litellm.py         # collect_litellm / collect_backend / discover_backends
    backend_count.py   # parse_api_base / count_via_* / resolve_backend_count (+GPU hook)
    gpu.py             # collect_gpu_for_service + Pod/Node GPU helpers
    snapshot.py        # build_snapshot / merge_deployments_with_health / summarize
    user_access.py     # collect_user_access / AccessCache / filter_snapshot_for_user
    prometheus.py      # render_prometheus_metrics (text exposition 0.0.4)
    demo.py            # demo_snapshot
    state.py           # SnapshotStore + async Refresher (replaces the old daemon threads)
  schemas/snapshot.py  # Pydantic response models (typed OpenAPI docs)
  api/routes.py        # /api/snapshot, /api/snapshot/user (POST), /snapshot.json, /metrics, /healthz
  web/routes.py        # / (dashboard), /snapshot.html (frozen page)
  web/templates/dashboard.html
test_model_monitor.py  # stays at repo root; imports the new modules via an `m.*` shim
```

## Commands

```bash
# Run tests (stdlib unittest — only imports app.core/app.services, no FastAPI needed)
python3 -m unittest -v
python3 -m unittest -v test_model_monitor.TestResolveBackendCount.test_kserve_rawdeployment_label_sum

# Install deps (FastAPI stack)
python3 -m pip install -r requirements.txt

# Run the service
uvicorn app.main:app --host 0.0.0.0 --port 8088
LITELLM_BASE_URL=http://litellm:4000 LITELLM_API_KEY=sk-1234 uvicorn app.main:app
python3 -m app                                            # uses Settings host/port

# Preview with NO live endpoints (sample data — also disables user-view)
MONITOR_DEMO=true uvicorn app.main:app --port 8088

# Build + push image (tag follows __version__ in app/__init__.py)
./ci.sh                  # docker build -> ai-tool/model-monitor:<version> + :latest
./push.sh                # retag to 10.92.20.77:5002 and push
```

There is no linter configured. The CI (`ci.sh`) only builds the Docker image — it does not run tests.

## Architecture / data flow

The core pipeline is `build_snapshot(settings)` → a single `snap` dict consumed identically by the JSON API, the dashboard JS, the frozen page, and the Prometheus renderer. Keep them in sync by changing the snapshot, not the renderers. `settings` is a plain dict (built by `config.build_collector_settings`); collectors and `K8sClient.from_settings` only see this dict, which is why unit tests can pass hand-written dicts.

1. **`collect_litellm`** (`services/litellm.py`) hits four LiteLLM endpoints. The critical and non-obvious mapping: `api_base` comes from `GET /model/info` (plaintext, needs an admin key), **not** from `/v1/models` (OpenAI-spec, names only). `/health` actively pings every backend and can take tens of seconds.

2. **Backend Pod counting** (`services/backend_count.resolve_backend_count` + `core/k8s.K8sClient`) — the central value-add. An `api_base` is a k8s Service (LB) address, so probing it only proves the LB is up, not how many Pods serve behind it. Tried in priority order:
   - KServe ISVC → Deployment by `serving.kserve.io/inferenceservice=<isvc>` label (`readyReplicas`/`replicas`)
   - EndpointSlice ready endpoints (activator Pods excluded)
   - Knative PodAutoscaler `actualScale`
   - Deployment `readyReplicas`/`replicas`
   - fall back to `?` — **never fabricate a count**. Failures record a `k8s_error`.

   `parse_api_base` turns an `api_base` URL into `(namespace, service)`; IPs/public domains classify as `external` and short-circuit. A `(ns, svc)` cache dedupes k8s lookups across deployments sharing a Service. Auto-enables in-cluster via the ServiceAccount token (`MONITOR_BACKEND_COUNT=false` disables).

   **Two caches with different lifetimes, and one that is deliberately absent.** `bc_cache` is built inside `build_snapshot` and thrown away each cycle (within-cycle dedup only). `node_cache` and `meta_cache` are owned by `Refresher` and live for the process — passed down through `build_snapshot(… , node_cache, meta_cache)` → `resolve_backend_count`. Measured steady state (35 deployments / 12 unique Services / 5s): **5 k8s calls per unique Service per cycle**, of which two answered the same thing every time. `meta_cache` (`services/gpu.META_TTL`, **60s**) removes those two — ~1.04M → ~0.66M calls/day. **The TTL is short for a wake-safety reason, not a load reason**: caching ISVC-absent leaves `network_type` at `service`, and `litellm._looks_kserve` is "name convention (`-predictor`) **or** `network_type==kserve`" — so for a Service that does not follow the naming convention the ISVC lookup is the *only* KServe signal. If such a Service gains a Serverless ISVC, `_deployment_health_safe` returns True for the whole TTL and LiteLLM's `/health?model=` pings (and wakes) that backend. The naming convention blocks it, but 60s keeps the window 12× the uncached 5s instead of 60×, for 8% less saving. Raise it only after re-doing that tradeoff. What it must **never** cache, each a real failure mode: an ISVC lookup that *succeeded* (its `revision` comes from `latestReadyRevision` and is dynamic — freezing it points the Knative PodAutoscaler lookup at a dead revision, so only `found: False` is cached); a non-404 ISVC failure (a transient RBAC/timeout would pin `network_type` at `-` for the whole TTL — same rule as `node_cache` only caching successes); and a `spec.selector` lookup that failed. The selector entry also **self-heals**: if a Pod query built from a cached selector returns zero items, the entry is dropped immediately rather than waiting out the TTL, because a relabeled redeploy looks exactly like that. The three remaining calls (EndpointSlice, Deployment status, Pod list) are genuinely dynamic — do not cache them.

   **GPU info** (`services/gpu.collect_gpu_for_service`, on by default, `MONITOR_GPU_INFO=false` / no Pod-Node RBAC → silent `?`): for each `(ns, svc)` it lists Pods (by KServe ISVC label, else the Service selector), sums `resources.limits["nvidia.com/gpu"]` over **Ready** Pods, and reads each Pod's node label `nvidia.com/gpu.product` for the device model (`H100`/`B200`), cached per node. Attaches `gpu_ready` + `gpu_products` (`{device: count}` — captures **heterogeneous GPU**) to the deployment, deduped by `(ns, svc)` in `summarize` like pod counts. Needs `pods` + `nodes` read RBAC.

3. **`merge_deployments_with_health`** joins `/model/info` (api_base) with `/health` (status) by api_base. When `/health` is missing/timed out, status falls back to k8s backend readiness (`backends_ready > 0` → UP, `scale_to_zero` → `?`). Sorted by name for stable order. **Admin pause overrides health**: `blocked is True` → `status="PAUSED"`, `status_source="blocked"`, and the health-derived verdict is preserved as `health_status` (so the UI can show `PAUSED(UP)` — will it actually come back?).

4. **`summarize`** computes the headline counts. Invariant covered by tests: cards (`deployments_healthy`/`unhealthy`) always equal the UP/DOWN rows, even when `/health` times out. **`PAUSED` is a third state counted in neither** (`deployments_blocked` counts it separately) — the identity holds because both sides count the same per-row status. `blocked_known` (any deployment carrying the key) says whether pause detection is even possible. **Backend Pod and GPU totals are deduped by `(namespace, service)`** — several `model_name`s can share one backend Service, so summing per row would double-count shared Pods/GPUs.

### Model pause / disable — `PAUSED` (LiteLLM `model_info.blocked`)
LiteLLM v1.90.0+ lets an admin pause a deployment without deleting it (`POST /model/block` / `/model/unblock`, `PATCH /model/{id}/update {"blocked":true}`, UI toggle). A blocked deployment leaves the routing pool and receives no traffic. `collect_litellm` reads it from **`model_info.blocked`** — the same sub-dict as `id`/`active_health_check` — using the shared strict `_parse_bool_marker` (never `bool()`-coerce; `"false"` must not become True).

**Why the monitor has to do this:** LiteLLM's `/health` does **not** filter blocked deployments (verified in v1.90.0 source) — a paused backend keeps getting pinged and reported healthy, so without this flag the dashboard shows a false UP for something serving zero traffic.

**Tri-state, and absent ≠ false.** The key exists only on DB-stored models; config.yaml-only models (and older LiteLLM) never carry it. Absent means *unknown* — the key is not created, and behavior is byte-identical to before. Never claim "active" from a missing key.

**Fully vs partially blocked** (mirrors LiteLLM's own `get_fully_blocked_model_names`): `select_health_check_models` skips a name only when **every** deployment under it is blocked (nothing to learn; don't poke what ops deliberately turned off). A partially blocked name is still checked — live siblings carry real traffic, and `/health?model=` is name-scoped so there's no way to ping only the live one. `blocked` is **not** a wake-danger signal, so it never pollutes `unsafe_underlying`/`unsafe_base`. Same rule in the dashboard's `compositeStatus` and graph node coloring: a shared backend is drawn paused only if *all* deployments pointing at it are paused.

**Known limitation:** LiteLLM hides a fully-blocked model name from `/v1/models`. The per-user access set is derived from the user's own `/v1/models`, so such a model *disappears* from the user view rather than showing as PAUSED (the admin view, built from `/model/info`, still shows it). Fixing that would mean widening the access set, which breaks fail-closed — so it stays.

### Background collection (no request ever blocks)
Collectors are synchronous (blocking `urllib`). The FastAPI **lifespan** starts an async `Refresher` (`services/state.py`) that runs them off the event loop via `asyncio.to_thread`: a fast refresh loop rebuilds the snapshot every `interval` **without** `/health`, and a separate slow `health_loop` fetches `/health` and injects it into the next snapshot. The latest snapshot lives in a `SnapshotStore`; handlers return it immediately.

**Selective health** (`MONITOR_SELECTIVE_HEALTH=true`, used when full `/health` is off): the full `/health` actively pings every backend, which wakes KServe Serverless (scale-to-zero) backends — that's why prod sets `MONITOR_HEALTH=false`. Both modes share one loop (`Refresher._health_loop(fetch_once)`); the selective fetcher (`_fetch_selective_health`) reads the latest snapshot and picks safe models via `select_health_check_models` (`services/litellm.py`). **KServe detection is the service-naming convention** (`-predictor`/`-transformer`/`-explainer` — an ops guarantee: every KServe-served svc is named `*-predictor`; update `_KSERVE_NAME_SUFFIXES` if that changes), falling back to the api_base hostname's first label so it works even with no k8s access. The rule: KServe-named (or ISVC-confirmed) → checked **only when k8s positively confirms `RawDeployment`** (no activator); positive Knative danger (`serverless`/`activator_only` booleans exported by `resolve_backend_count`, scale-to-zero, knative-* count source) → never checked, not even with the marker; everything else — plain Services **and external IPs** — is checked (the ping goes through LiteLLM, the monitor never contacts backends directly). A name is also excluded when any unsafe sibling shares its **underlying model (provider-prefix-normalized) or api_base** — observed in prod: LiteLLM's `?model=` matches more broadly than model_name and can ping a Serverless sibling. Calls run per model via `asyncio.gather` + Semaphore(4) over `fetch_health_for_model` (per-call timeout capped at 30s; no inner thread pool — stays inside main.py's `_COLLECT_THREADS` budget). `aggregate_selective_health` merges responses into the /health shape: dedup by (model, api_base) with **DOWN winning contradictions**, endpoints filtered to each model's own api_bases (defends against a LiteLLM that ignores `?model=`), and returns **None when every call failed** so the last good health is never clobbered (same contract as `fetch_health`). Its `errors` are surfaced into `litellm.errors` (capped) at injection so systemic failure is visible on the dashboard. **LiteLLM returns HTTP 503 (not 200) with the same health payload when the checked target is unhealthy** — `http_get_json` parses JSON error bodies and both fetchers accept health-shaped 503 bodies, otherwise every DOWN backend would be recorded as a fetch error instead of a DOWN status. Per-model manual override: LiteLLM `model_info.active_health_check` — parsed strictly (bool or "true"/"false"-style strings only; never `bool()`-coerced), false=always skip; true=allow undetermined/external, but cannot override a positive Knative signal. Full `/health` (`MONITOR_HEALTH=true`) takes precedence — the selective fetcher then isn't used.

### Model-grouped view & Model↔Backend graph (web only)
The dashboard JS groups deployments by `model_name` (composite `UP`/`DEGRADED`/`DOWN` + `Σ ready/desired`, child rows per backend), flags shared backends (`⇄`), and draws a pure-SVG bipartite **Model ↔ Backend** graph. This logic lives entirely in `web/templates/dashboard.html` (no extra snapshot data; derived from `model_name`/`namespace`/`service`/`status`). The old single-file had Python TUI equivalents — those were **not** ported (no TUI).

### 지금 부하 (`app/services/load.py`, `MONITOR_LOAD`, 기본 ON)
backend Pod 마다 `/metrics`(vLLM/SGLang 게이지)를 읽어 처리 중/대기 요청 · KV 캐시 사용률 ·
tok/s · 등급(idle/ok/BUSY/FULL)을 deployment 행에 붙인다. Pod 주소는 `gpu.collect_gpu_for_service`
가 이미 받아오는 Pod 목록(`pod_targets`)에서 나오므로 **k8s 호출이 늘지 않는다**.

- **깨울 위험이 있으면 조회하지 않는다.** Pod 주소가 없을 때 LB(api_base)로 폴백하면 activator 를
  거쳐 scale-to-zero 백엔드를 깨운다 — 전량 `/health` 를 기본 off 로 둔 것과 같은 이유다.
  `litellm._deployment_health_safe` 로 판정하고, external 백엔드도 제외한다. 제외된 대상은 0 이
  아니라 **이유를 단 `unknown`**(`scope="skipped"`).
- **자체 스레드풀 금지.** `Refresher._fetch_load` 가 공용 스레드 예산(semaphore 4) 안에서 모아
  느린 `/health` 와 같은 방식으로 주입한다. tok/s 카운터 차분 캐시(`_tput_cache`)도 Refresher 수명.
- **엔진 차이는 `_PROM_SPECS` 한 곳에서** — `:`↔`_` 접두사 정규화(SGLang), alias 는 합산 아닌 택일
  (vLLM V0/V1 동시 노출 시 2배 방지), `tp_rank`/`pp_rank`/`moe_ep_rank` 는 복제 보고라 max 로 접고
  `dp_rank`/`engine` 만 합산(TP=4 에서 4배 부풀림 방지).
- **모델 등급은 라우팅 방식에 따른다**(`MONITOR_LOAD_ROUTING`): least-busy(기본)=가장 한가한 backend,
  shuffle=가장 나쁜 backend. `summarize` 와 대시보드가 **같은 규칙**을 써야 카드와 표가 안 어긋난다.
- **주기 60초**(`MONITOR_LOAD_INTERVAL`, 스냅샷 갱신 5초와 분리) + 화면에 `N초 전` 표기. 즉시
  갱신은 `POST /api/load/refresh`(`Refresher.refresh_load_now`) — 요청 경로에서 실제 수집하는
  유일한 예외라 최소 간격 10초 + 진행 중 락으로 직렬화한다. 백그라운드 루프는 **세션 수와 무관하게
  하나**이므로 보는 사람이 늘어도 백엔드 조회는 늘지 않는다.
- per-user 뷰에는 평탄한 스칼라(`load_state`/`load_running`/...)로만 나간다 — `per_pod` 에 Pod 주소가
  있어 그대로 넘기면 내부가 샌다. 사유도 원문 대신 정규화 코드(`load_reason_code`).
  **노출 단계는 설정**(`MONITOR_USER_VIEW_LOAD` / `user_view.load`): `off`(키 자체 없음 + 최상위
  `load_enabled` 등 `_LOAD_TOP_KEYS` 도 제거 → 대시보드 LOAD 컬럼이 사라진다) / `summary`(등급 +
  `load_reason_code` + `load_partial` 불리언만) / `detail`(수치까지). **기본은 `summary`** — 오타 등
  모르는 값도 `summary` 로 떨어진다(`config.normalize_user_load`, fail-safe 방향). 화면에서 가리는
  것이 아니라 **서버에서 빼는 것**이고, `show_internal` 뷰(원본 행)에도 같은 규칙이 적용된다
  (`_slim_load`). `summarize` 의 **`load_state_known` 은 `load_known`(수치)과 분리**돼 있다 —
  summary 모드는 수치가 없어 한 플래그로 묶으면 등급 카드까지 사라진다. `⟳ 부하`(수동 갱신)는
  백엔드 팬아웃이라 admin 전용이고 비-admin 화면에서는 버튼 자체를 숨긴다.

### Per-user (key) view — `MONITOR_USER_VIEW=true` (off by default; demo disables it)
"Key-required mode": the user enters their own LiteLLM key (header `X-LiteLLM-Key` only — never query/logs/server-store; browser `sessionStorage`). `POST /api/snapshot/user` filters the shared snapshot per key via `services/user_access.filter_snapshot_for_user` (access set from that key's `GET /v1/models`, cached with a short TTL in `AccessCache` — sha256 of the key, success-only). A normal key sees only its models with internal `api_base`/namespace **redacted**; the admin key (= the monitor's own `api_key`, constant-time compared in `auth.is_admin_key`) sees the full view + exports. **fail-closed**: an invalid key never falls back to global. When on, `GET /api/snapshot` is 403-locked and `/snapshot.json`, `/snapshot.html`, `/metrics` require the admin key header. Template placeholder `__USER_VIEW__` is injected by `web/routes.load_dashboard_html` (alongside `__INTERVAL_MS__`).

### Prometheus `/metrics`
`services/prometheus.render_prometheus_metrics(snap)` formats the cached snapshot as text exposition 0.0.4 (no collection on the scrape path). Status encoded UP=1/DOWN=0/idle=-1 (**`PAUSED` also lands on -1** — it is exposed separately as `model_monitor_model_blocked` 1/0 plus summary `model_monitor_deployments_blocked` / `model_monitor_blocked_known`, deliberately so that existing `model_up == 0` alerts never fire on a deliberately paused model); duplicate label series (one `model_name`, several deployments) are collapsed (`_dedup_samples`, DOWN wins); `api_base` is never a label. `deploy/grafana-dashboard.json` + `deploy/prometheus-alerts.yaml` ship ready-to-use.

### Endpoints
`/` (HTML), `/api/snapshot` (live JSON; locked under user-view), `/api/snapshot/user` (POST, key-filtered), `/snapshot.json` (download), `/snapshot.html` (frozen self-contained page), `/metrics` (Prometheus), `/healthz` + `/readyz`. Only `/api/snapshot` and `/api/snapshot/user` appear in the OpenAPI schema.

### Settings precedence
env (`LITELLM_BASE_URL`, `LITELLM_API_KEY`, `MONITOR_*`) > config file (`MONITOR_CONFIG_FILE`) > default — resolved in `config.build_collector_settings`. The file supplies nested settings (`backend_count.*`, `backends`, `namespace_overrides`, `user_view.*`, `metrics.*`). Config is `.json` (always) or `.yaml` (PyYAML only). The old CLI flags are gone.

## Branch strategy

`main` is split into two long-lived lines, and feature work is branched off separately:

- **`develop`** — integration branch for ongoing development.
- **`product`** — branch tracking the product/release line.
- **`feature/<name>`** — one branch per feature (branch off `develop`). Do feature development here, not directly on `develop`/`product`/`main`.

### Tagging on merge (automated)
`.github/workflows/tag-on-merge.yml` tags `develop`/`product` on every merge (push), using `__version__` from `app/__init__.py`:
- **`product`** → `v<version>` — immutable release tag. **Fails the workflow if the tag already exists**, forcing a `__version__` bump before a product release.
- **`develop`** → `v<version>-develop` — floating pre-release tag, **force-moved** to the latest commit on each develop merge (so it always points at the newest develop snapshot for that version).

So: bump `__version__` for a product release; develop merges just re-point the `-develop` tag. Tag pushes don't re-trigger the branch workflow (no loop).

## Conventions

- **Versioning:** `__version__` in `app/__init__.py` is the single source of truth for the *runtime* — it drives the Docker image tag (`ci.sh`/`push.sh` grep it), the FastAPI app `version`, the `version` field in `/api/snapshot`, and `model_monitor_build_info`. But two files carry a **manually-mirrored copy** that is *not* auto-derived and must be bumped by hand alongside it: the README header (`**버전: vX.Y.Z**`) and `deploy/k8s.yaml`'s `app.kubernetes.io/version` label (**2 occurrences**). `TestVersionConsistency` (in `test_model_monitor.py`) fails if any of these drift from `__version__` — run `python3 -m unittest` after a bump.
- **Schemas vs. dicts:** the snapshot is built and tested as plain dicts; `schemas/snapshot.py` Pydantic models only document/validate at the API boundary (all fields Optional, `extra="allow"` so nothing is dropped). Don't push Pydantic into the collectors.
### Per-user view cost
`filter_snapshot_for_user` **filters before it copies**. It used to `deepcopy` the whole snapshot and then filter, which made the cost proportional to the *total* deployment count regardless of what the key could see — paid once per poll per user (measured: 1000 deployments / 50 accessible, 13.7ms → 0.24ms; 5.5× even at full access). The tradeoff is that isolation is no longer automatic: anything carried over from the shared snapshot must be deep-copied if it is a container, or every user's view aliases the one snapshot every other request reads. Two regression tests pin that (mutate the view, assert the global is byte-identical; assert nested `gpu_products` dicts are not shared). Do not "optimize" this into a cross-user cache — `backend_ref` is salted per user (`sha256(admin_key + key)`) precisely so two users cannot correlate their views, so a shared cache would either break that or need the salt applied after the shared work.

- Tests pin the fiddliest logic (`parse_api_base`, `resolve_backend_count`, GPU, `merge_deployments_with_health`, `summarize`, `filter_snapshot_for_user`, `AccessCache`, `render_prometheus_metrics`). Use the `FakeClient` pattern (route by path substring). The suite imports only `app.core`/`app.services` via the `m.*` shim (no FastAPI). Add regression tests when touching parsing/merge/count/filter/metrics logic.
- Comments and user-facing strings are in Korean — match the surrounding language when editing.
- One k8s/backend failure must never abort the whole snapshot: per-deployment collection is wrapped in try/except that records the error and continues. The per-user filter must operate on a **deepcopy** — never mutate the shared global snapshot.
