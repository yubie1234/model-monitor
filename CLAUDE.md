# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`model-monitor` is a single-file monitor for a LiteLLM → KServe → vLLM/SGLang stack. It answers three questions: **which models are actually serving**, **how many backend Pods sit behind each `api_base` (load balancer)**, and — the primary one — **is a model busy right now** (in-flight/queued requests, KV cache pressure, tok/s, read per Pod from the engine's own gauges). It renders to a terminal (TUI), a web dashboard (`--serve`), or JSON.

**Default constraint: zero third-party dependencies — Python 3.6+ standard library only.** The whole point is that `model_monitor.py` runs on an air-gapped node with no `pip install`. Default to stdlib imports (`http.server`, `urllib`, `ssl`, `json`, `argparse`, `threading`, etc.). PyYAML is used *only if already present* (config loading degrades to JSON otherwise) — never make it a hard requirement. **If a task genuinely needs an external package, do not add it silently — ask the user first** and confirm it's acceptable given the air-gapped deployment target before introducing the dependency.

Almost all code lives in **`model_monitor.py`** (~2450 lines). There is no package structure.

## Commands

```bash
# Run tests (stdlib unittest, no deps)
python3 -m unittest -v
python3 test_model_monitor.py                          # equivalent

# Run a single test
python3 -m unittest -v test_model_monitor.TestResolveBackendCount.test_kserve_rawdeployment_label_sum

# Run the app against live endpoints
python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234
python3 model_monitor.py --config config.yaml --watch          # live refresh
python3 model_monitor.py --config config.yaml --serve --port 8088   # web dashboard

# Develop/preview with NO live endpoints (sample data)
python3 model_monitor.py --demo
python3 model_monitor.py --demo --serve --port 8088

# Build + push image (tag follows __version__ in model_monitor.py)
./ci.sh                  # docker build -> ai-tool/llm-monitor:<version> + :latest
./push.sh                # retag to 10.92.20.77:5002 and push
```

There is no linter configured. The CI (`ci.sh`) only builds the Docker image — it does not run tests.

## Architecture / data flow

The core pipeline is `build_snapshot(settings)` → a single `snap` dict consumed identically by the TUI (`render`), web (`serve_dashboard`), and `--json`. Keep all three in sync by changing the snapshot, not the renderers.

1. **`collect_litellm`** hits four LiteLLM endpoints. The critical and non-obvious mapping: `api_base` comes from `GET /model/info` (plaintext, needs an admin key), **not** from `/v1/models` (OpenAI-spec, names only). `/health` actively pings every backend and can take tens of seconds.

2. **Backend Pod counting** (`resolve_backend_count` + `K8sClient`) — the central value-add. An `api_base` is a k8s Service (LB) address, so probing it only proves the LB is up, not how many Pods serve behind it. That count comes from the control plane, tried in priority order:
   - KServe ISVC → Deployment by `serving.kserve.io/inferenceservice=<isvc>` label (`readyReplicas`/`replicas`)
   - EndpointSlice ready endpoints (activator Pods excluded)
   - Knative PodAutoscaler `actualScale`
   - Deployment `readyReplicas`/`replicas`
   - fall back to `?` — **never fabricate a count**. Failures record a `k8s_error` for the UI tooltip.

   `parse_api_base` turns an `api_base` URL into `(namespace, service)`; IPs/public domains classify as `external` and short-circuit (no k8s calls). A `(ns, svc)` cache dedupes k8s lookups across deployments sharing a Service. The k8s client auto-enables in-cluster via the ServiceAccount token (`--no-backend-count` disables).

3. **현재 부하** (`load_targets` → `collect_load` → `aggregate_pod_loads` → `classify_load`) — 이 도구의 1급
   지표다. LB(`api_base`)로 `/metrics` 를 찌르면 뒤에 있는 Pod 중 하나만 응답하므로, backend 개수를 셀 때
   EndpointSlice 에서 **Pod 주소(`backend_pods`)를 함께 얻어 Pod 마다 직접** 조회한다(스레드 병렬).
   집계는 RUN/QUEUE 는 합, KV 는 최댓값(+평균). 등급은 큐 우선(대기 ≥1 → BUSY, ≥5 또는 KV ≥95% → FULL);
   게이지를 못 읽으면 `unknown` 이지 0 이 아니다. Pod 주소가 없으면 LB 1회 샘플링하고 `scope="lb-sample"`
   로 **표본을 드러낸다**(UI 의 `PODS` 열). tok/s 는 생성 토큰 카운터를 직전 샘플과 차분해서 내므로
   watch/serve 의 2번째 갱신부터 나온다(`_TPUT_HISTORY`). 기본 ON, `--no-load` 로 끈다.

4. **누적 사용량** (`collect_usage` + `attach_usage_to_deployments`) — 모델별 요청 수/토큰. **기본 OFF**
   (`--usage` 로 켬) — "지금 바쁜가"와 다른 축이고 LiteLLM DB 가 있어야 한다. LiteLLM 은 버전마다
   분석 엔드포인트가 달라서 후보(`/global/activity/model` → `/gateway/daily/activity` → `/model/metrics`)를
   **우선순위로 시도하고 처음 데이터가 나온 응답만** 정규화한다(`usage["source"]` 에 기록). 전부 실패하면
   비워 두고 UI 에서 열이 사라진다 — backend 개수와 같은 원칙으로 **추정값을 넣지 않는다**. 사용량은
   `model_name`(그룹) 단위라 합계는 반드시 `usage["totals"]` 를 쓴다(행을 더하면 replica 만큼 중복 — 테스트로 고정).
   전제 두 가지: LiteLLM 에 DB 가 있어야 하고(요청 수는 `LiteLLM_SpendLogs` 집계), 키 role 에 따라
   범위가 스코프된다(internal user 키면 본인 몫만 — 그래서 `/user/daily/activity` 는 후보에서 뺐다).
   rpm 은 LiteLLM 이 주는 값이 아니라 `요청 수 ÷ 구간` 으로 계산한 구간 평균이다.
   `--probe-metrics` 를 켜면 백엔드 `/metrics`(vLLM/SGLang Prometheus 게이지)에서 현재 실행/대기 요청과
   KV 캐시 사용률을 읽어 붙인다(LB 경유라 Pod 1개 샘플이라는 점을 UI 에 표기).

5. **`merge_deployments_with_health`** joins `/model/info` (api_base) with `/health` (status) by api_base. When `/health` is missing/timed out, status falls back to k8s backend readiness (`backends_ready > 0` → UP, `scale_to_zero` → `?`). Deployments are sorted by name so output order is stable across LiteLLM responses.

6. **`summarize`** computes the headline counts. Invariant covered by tests: the dashboard cards (`deployments_healthy`/`unhealthy`) must always equal the UP/DOWN rows in the table, even when `/health` times out.

### Web dashboard threading
`--serve` does **not** collect on the request path. A background `refresh_loop` rebuilds the snapshot on an interval and `/health` is collected in a *separate* `health_loop` thread (because it's slow), then injected. HTTP handlers return the last cached snapshot immediately — no request ever blocks on collection. Endpoints: `/` (HTML), `/api/snapshot` (live JSON), `/snapshot.json` (download), `/snapshot.html` (self-contained frozen page).

### Settings precedence
CLI args > env (`LITELLM_BASE_URL`, `LITELLM_API_KEY`) > config file. Resolved once in `resolve_settings`. Config is `.json` (always works) or `.yaml` (only if PyYAML present).

## Branch strategy

`main` is split into two long-lived lines, and feature work is branched off separately:

- **`develop`** — integration branch for ongoing development.
- **`product`** — branch tracking the product/release line.
- **`feature/<name>`** — one branch per feature (e.g. `feature/per-user-dashboard`). Do feature development here, not directly on `develop`/`product`/`main`.

## Conventions

- **Versioning:** `__version__` in `model_monitor.py` is the single source of truth — it drives the Docker image tag (`ci.sh`/`push.sh` grep it), the `--version` flag, and the `version` field in TUI/web/`/api/snapshot`. Bump it there; the README header version should follow.
- Tests deliberately pin the fiddliest logic (KServe/Knative-version-sensitive `parse_api_base`, `resolve_backend_count`, `merge_deployments_with_health`, `summarize`). Use the `FakeClient` pattern (route by path substring) instead of real k8s. Add regression tests here when touching parsing/merge/count logic.
- Comments and user-facing strings are in Korean — match the surrounding language when editing.
- One k8s/backend failure must never abort the whole snapshot: per-deployment collection is wrapped in try/except that records the error and continues.
