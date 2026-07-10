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

   **GPU info** (`services/gpu.collect_gpu_for_service`, on by default, `MONITOR_GPU_INFO=false` / no Pod-Node RBAC → silent `?`): for each `(ns, svc)` it lists Pods (by KServe ISVC label, else the Service selector), sums `resources.limits["nvidia.com/gpu"]` over **Ready** Pods, and reads each Pod's node label `nvidia.com/gpu.product` for the device model (`H100`/`B200`), cached per node. Attaches `gpu_ready` + `gpu_products` (`{device: count}` — captures **heterogeneous GPU**) to the deployment, deduped by `(ns, svc)` in `summarize` like pod counts. Needs `pods` + `nodes` read RBAC.

3. **`merge_deployments_with_health`** joins `/model/info` (api_base) with `/health` (status) by api_base. When `/health` is missing/timed out, status falls back to k8s backend readiness (`backends_ready > 0` → UP, `scale_to_zero` → `?`). Sorted by name for stable order.

4. **`summarize`** computes the headline counts. Invariant covered by tests: cards (`deployments_healthy`/`unhealthy`) always equal the UP/DOWN rows, even when `/health` times out. **Backend Pod and GPU totals are deduped by `(namespace, service)`** — several `model_name`s can share one backend Service, so summing per row would double-count shared Pods/GPUs.

### Background collection (no request ever blocks)
Collectors are synchronous (blocking `urllib`). The FastAPI **lifespan** starts an async `Refresher` (`services/state.py`) that runs them off the event loop via `asyncio.to_thread`: a fast refresh loop rebuilds the snapshot every `interval` **without** `/health`, and a separate slow `health_loop` fetches `/health` and injects it into the next snapshot. The latest snapshot lives in a `SnapshotStore`; handlers return it immediately.

**Selective health** (`MONITOR_SELECTIVE_HEALTH=true`, used when full `/health` is off): the full `/health` actively pings every backend, which wakes KServe Serverless (scale-to-zero) backends — that's why prod sets `MONITOR_HEALTH=false`. Both modes share one loop (`Refresher._health_loop(fetch_once)`); the selective fetcher (`_fetch_selective_health`) reads the latest snapshot, picks only models whose **every** deployment is positively safe to ping (`select_health_check_models` in `services/litellm.py`: KServe RawDeployment, or plain Service **with a positive non-Knative pod count** — never serverless/activator_only/scale-to-zero/knative-* source/external/undetermined; the `serverless`/`activator_only` booleans are exported by `resolve_backend_count`, the single source of Knative truth), then calls `/health?model=<name>` per model via `asyncio.gather` + Semaphore(4) over `fetch_health_for_model` (per-call timeout capped at 30s; no inner thread pool — stays inside main.py's `_COLLECT_THREADS` budget). `aggregate_selective_health` merges responses into the /health shape: dedup by (model, api_base) with **DOWN winning contradictions**, endpoints filtered to each model's own api_bases (defends against a LiteLLM that ignores `?model=`), and returns **None when every call failed** so the last good health is never clobbered (same contract as `fetch_health`). Its `errors` are surfaced into `litellm.errors` (capped) at injection so systemic failure is visible on the dashboard. **LiteLLM returns HTTP 503 (not 200) with the same health payload when the checked target is unhealthy** — `http_get_json` parses JSON error bodies and both fetchers accept health-shaped 503 bodies, otherwise every DOWN backend would be recorded as a fetch error instead of a DOWN status. Per-model manual override: LiteLLM `model_info.active_health_check` — parsed strictly (bool or "true"/"false"-style strings only; never `bool()`-coerced), false=always skip; true=allow undetermined/external, but cannot override a positive Knative signal. Full `/health` (`MONITOR_HEALTH=true`) takes precedence — the selective fetcher then isn't used.

### Model-grouped view & Model↔Backend graph (web only)
The dashboard JS groups deployments by `model_name` (composite `UP`/`DEGRADED`/`DOWN` + `Σ ready/desired`, child rows per backend), flags shared backends (`⇄`), and draws a pure-SVG bipartite **Model ↔ Backend** graph. This logic lives entirely in `web/templates/dashboard.html` (no extra snapshot data; derived from `model_name`/`namespace`/`service`/`status`). The old single-file had Python TUI equivalents — those were **not** ported (no TUI).

### Per-user (key) view — `MONITOR_USER_VIEW=true` (off by default; demo disables it)
"Key-required mode": the user enters their own LiteLLM key (header `X-LiteLLM-Key` only — never query/logs/server-store; browser `sessionStorage`). `POST /api/snapshot/user` filters the shared snapshot per key via `services/user_access.filter_snapshot_for_user` (access set from that key's `GET /v1/models`, cached with a short TTL in `AccessCache` — sha256 of the key, success-only). A normal key sees only its models with internal `api_base`/namespace **redacted**; the admin key (= the monitor's own `api_key`, constant-time compared in `auth.is_admin_key`) sees the full view + exports. **fail-closed**: an invalid key never falls back to global. When on, `GET /api/snapshot` is 403-locked and `/snapshot.json`, `/snapshot.html`, `/metrics` require the admin key header. Template placeholder `__USER_VIEW__` is injected by `web/routes.load_dashboard_html` (alongside `__INTERVAL_MS__`).

### Prometheus `/metrics`
`services/prometheus.render_prometheus_metrics(snap)` formats the cached snapshot as text exposition 0.0.4 (no collection on the scrape path). Status encoded UP=1/DOWN=0/idle=-1; duplicate label series (one `model_name`, several deployments) are collapsed (`_dedup_samples`, DOWN wins); `api_base` is never a label. `deploy/grafana-dashboard.json` + `deploy/prometheus-alerts.yaml` ship ready-to-use.

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
- Tests pin the fiddliest logic (`parse_api_base`, `resolve_backend_count`, GPU, `merge_deployments_with_health`, `summarize`, `filter_snapshot_for_user`, `AccessCache`, `render_prometheus_metrics`). Use the `FakeClient` pattern (route by path substring). The suite imports only `app.core`/`app.services` via the `m.*` shim (no FastAPI). Add regression tests when touching parsing/merge/count/filter/metrics logic.
- Comments and user-facing strings are in Korean — match the surrounding language when editing.
- One k8s/backend failure must never abort the whole snapshot: per-deployment collection is wrapped in try/except that records the error and continues. The per-user filter must operate on a **deepcopy** — never mutate the shared global snapshot.
