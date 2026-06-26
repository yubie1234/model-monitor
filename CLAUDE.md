# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`model-monitor` is a single-file monitor for a LiteLLM → KServe → vLLM/SGLang stack. It answers two questions: **which models are actually serving**, and **how many backend Pods sit behind each `api_base` (load balancer)**. It renders to a terminal (TUI), a web dashboard (`--serve`), or JSON.

**Hard constraint: zero third-party dependencies — Python 3.6+ standard library only.** The whole point is that `model_monitor.py` runs on an air-gapped node with no `pip install`. Do not add imports outside the stdlib (`http.server`, `urllib`, `ssl`, `json`, `argparse`, `threading`, etc.). PyYAML is used *only if already present* (config loading degrades to JSON otherwise) — never make it a requirement.

Almost all code lives in **`model_monitor.py`** (~1650 lines). There is no package structure.

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

3. **`merge_deployments_with_health`** joins `/model/info` (api_base) with `/health` (status) by api_base. When `/health` is missing/timed out, status falls back to k8s backend readiness (`backends_ready > 0` → UP, `scale_to_zero` → `?`). Deployments are sorted by name so output order is stable across LiteLLM responses.

4. **`summarize`** computes the headline counts. Invariant covered by tests: the dashboard cards (`deployments_healthy`/`unhealthy`) must always equal the UP/DOWN rows in the table, even when `/health` times out.

### Web dashboard threading
`--serve` does **not** collect on the request path. A background `refresh_loop` rebuilds the snapshot on an interval and `/health` is collected in a *separate* `health_loop` thread (because it's slow), then injected. HTTP handlers return the last cached snapshot immediately — no request ever blocks on collection. Endpoints: `/` (HTML), `/api/snapshot` (live JSON), `/snapshot.json` (download), `/snapshot.html` (self-contained frozen page).

### Settings precedence
CLI args > env (`LITELLM_BASE_URL`, `LITELLM_API_KEY`) > config file. Resolved once in `resolve_settings`. Config is `.json` (always works) or `.yaml` (only if PyYAML present).

## Conventions

- **Versioning:** `__version__` in `model_monitor.py` is the single source of truth — it drives the Docker image tag (`ci.sh`/`push.sh` grep it), the `--version` flag, and the `version` field in TUI/web/`/api/snapshot`. Bump it there; the README header version should follow.
- Tests deliberately pin the fiddliest logic (KServe/Knative-version-sensitive `parse_api_base`, `resolve_backend_count`, `merge_deployments_with_health`, `summarize`). Use the `FakeClient` pattern (route by path substring) instead of real k8s. Add regression tests here when touching parsing/merge/count logic.
- Comments and user-facing strings are in Korean — match the surrounding language when editing.
- One k8s/backend failure must never abort the whole snapshot: per-deployment collection is wrapped in try/except that records the error and continues.
