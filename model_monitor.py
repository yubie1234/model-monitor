#!/usr/bin/env python3
"""
model_monitor.py — LiteLLM -> KServe -> vLLM/SGLang 백엔드에서 실제로 떠 있는 모델 현황을 조회하는 모니터.

특징
  - 외부 패키지 0개 (Python 3.6+ 표준 라이브러리만 사용 -> air-gapped 노드에서 설치 없이 실행)
  - 데이터 소스
      * LiteLLM gateway:  GET /model_group/info  (등록된 모델 그룹)
                          GET /health             (백엔드 실제 health = "떠 있음"의 근거)
                          GET /v1/models          (OpenAI 호환 모델 목록)
                          GET /global/activity/model 등 (모델별 요청 수/토큰 = 사용량)
      * (옵션) 백엔드 직접 probe: 각 vLLM/SGLang 엔드포인트의 GET /v1/models, /health
      * (옵션) 백엔드 GET /metrics: 현재 실행/대기 요청, KV 캐시 사용률(--probe-metrics)
  - 출력: 1회 스냅샷 / --json / --watch(실시간) 지원
  - --demo: 라이브 엔드포인트 없이 샘플 데이터로 출력 미리보기

사용 예
  python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234
  python3 model_monitor.py --config config.yaml --watch
  python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234 --json
  python3 model_monitor.py --demo --watch
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

__version__ = "0.2.0"

# ----------------------------------------------------------------------------
# HTTP (stdlib only)
# ----------------------------------------------------------------------------


def http_get_text(url, api_key=None, timeout=10, accept="application/json"):
    """GET url -> (ok: bool, text: str|None, error: str|None). 본문을 그대로 반환."""
    headers = {"Accept": accept}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
        headers["x-api-key"] = api_key  # LiteLLM accepts either
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, None, "HTTP %s %s %s" % (e.code, e.reason, body)
    except urllib.error.URLError as e:
        return False, None, "connection error: %s" % e.reason
    except Exception as e:  # noqa: BLE001
        return False, None, "%s: %s" % (type(e).__name__, e)


def http_get_json(url, api_key=None, timeout=10):
    """GET url -> (ok: bool, data: dict|list|None, error: str|None)."""
    ok, raw, err = http_get_text(url, api_key, timeout)
    if not ok:
        return False, None, err
    try:
        return True, json.loads(raw), None
    except ValueError:
        return False, None, "non-JSON response: %s" % raw[:200]


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


def load_config(path):
    """JSON 우선, .yaml/.yml 은 PyYAML 있으면 사용. 둘 다 안되면 안내."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit(
                "config '%s' 는 YAML 인데 PyYAML 이 없습니다. "
                "JSON 설정(.json)을 쓰거나 PyYAML 을 설치하세요." % path
            )
        return yaml.safe_load(text) or {}
    return json.loads(text)


def resolve_settings(args):
    """CLI > env > config 파일 순으로 설정 병합."""
    cfg = {}
    if args.config:
        cfg = load_config(args.config) or {}

    litellm = cfg.get("litellm", {}) if isinstance(cfg.get("litellm"), dict) else {}
    bc = cfg.get("backend_count", {}) if isinstance(cfg.get("backend_count"), dict) else {}
    ug = cfg.get("usage", {}) if isinstance(cfg.get("usage"), dict) else {}

    # backend 개수 수집(k8s API) 사용 여부:
    #   --no-backend-count 면 off, 기본은 auto(= in-cluster SA 토큰 있으면 자동 on)
    bc_enabled = True
    if getattr(args, "no_backend_count", False):
        bc_enabled = False

    settings = {
        "litellm_url": (
            args.litellm_url
            or os.environ.get("LITELLM_BASE_URL")
            or litellm.get("url")
        ),
        "api_key": (
            args.api_key
            or os.environ.get("LITELLM_API_KEY")
            or litellm.get("api_key")
        ),
        "backends": cfg.get("backends", []),  # [{name,url,type}]
        "probe_backends": args.probe_backends or bool(cfg.get("probe_backends")),
        "timeout": args.timeout,
        # /health 는 백엔드를 전부 ping 해서 느림(수십 초) -> 별도 큰 타임아웃
        "health": (not getattr(args, "no_health", False)
                   and litellm.get("health", True)),
        "health_timeout": float(
            args.health_timeout or litellm.get("health_timeout") or 90.0),
        # --- backend 개수(LB 뒤 Pod 수) 수집 설정 ---
        "backend_count": bc_enabled,
        "k8s_api_server": (args.k8s_api_server or bc.get("api_server")),
        "k8s_token_file": (args.k8s_token_file or bc.get("token_file")
                           or "/var/run/secrets/kubernetes.io/serviceaccount/token"),
        "k8s_ca_file": (args.k8s_ca_file or bc.get("ca_file")
                        or "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
        "k8s_insecure": bool(args.k8s_insecure or bc.get("insecure")),
        "k8s_timeout": float(args.k8s_timeout or bc.get("timeout") or 5.0),
        "default_namespace": bc.get("default_namespace"),
        "namespace_overrides": bc.get("namespace_overrides", {}) or {},
        "activator_namespace": bc.get("activator_namespace", "knative-serving"),
        # --- 사용량(요청 수/토큰/사용률) ---
        #  LiteLLM 분석 엔드포인트 1~2회 호출. 권한/버전 문제로 실패해도 나머지는 그대로.
        "usage": (not getattr(args, "no_usage", False)
                  and ug.get("enabled", True)),
        "usage_window": float(args.usage_window or ug.get("window_hours") or 24.0),
        # 백엔드 /metrics(Prometheus) 직접 probe -> 현재 실행/대기 요청, KV 캐시 사용률
        "probe_metrics": bool(getattr(args, "probe_metrics", False)
                              or cfg.get("probe_metrics")),
    }
    return settings


# ----------------------------------------------------------------------------
# Collectors
# ----------------------------------------------------------------------------


def _classify_backend(model_name, underlying, api_base):
    """model_name 접두사/underlying model 로 백엔드 종류 추정 (표시용)."""
    blob = ("%s %s %s" % (model_name, underlying, api_base)).lower()
    if "sglang" in blob:
        return "sglang"
    if "kserve" in blob:
        return "kserve"
    if "vllm" in blob:
        return "vllm"
    return "-"


def fetch_health(url, api_key, timeout):
    """LiteLLM /health 만 단독 조회 (느려서 비동기로 돌릴 때 사용). dict|None."""
    ok, data, err = http_get_json(url.rstrip("/") + "/health", api_key, timeout)
    return data if (ok and isinstance(data, dict)) else None


def collect_litellm(url, api_key, timeout, health_timeout=None, with_health=True):
    """LiteLLM 게이트웨이에서 모델 그룹 + deployment(api_base) + health 수집.

    핵심: api_base 는 /v1/models 가 아니라 /model/info 에서 나온다.
    /health 는 모든 백엔드를 실제 ping 하므로 느리다(수십 초). with_health=False 면
    건너뛰고, 호출측에서 fetch_health 로 비동기 수집해 주입한다.
    """
    if health_timeout is None:
        health_timeout = max(timeout, 90.0)
    base = url.rstrip("/")
    result = {
        "url": base,
        "reachable": False,
        "groups": [],          # model_group/info data
        "deployments": [],     # /model/info 정규화: model_name -> api_base 등
        "health": None,        # /health raw
        "models": [],          # /v1/models ids (이름만)
        "errors": [],
    }

    ok, data, err = http_get_json(base + "/model_group/info", api_key, timeout)
    if ok and isinstance(data, dict):
        result["reachable"] = True
        # 이름순 정렬: LiteLLM 응답 순서가 replica 구성에 따라 바뀌어도 표시 고정
        result["groups"] = sorted(
            data.get("data", []) or [],
            key=lambda g: str(g.get("model_group") or "").lower())
    elif err:
        result["errors"].append("model_group/info: %s" % err)

    # /model/info -> 실제 api_base 가 여기서 나온다 (admin 권한 키 권장)
    ok, data, err = http_get_json(base + "/model/info", api_key, timeout)
    if ok and isinstance(data, dict):
        result["reachable"] = True
        for d in data.get("data", []) or []:
            lp = d.get("litellm_params", {}) or {}
            mi = d.get("model_info", {}) or {}
            name = d.get("model_name", "?")
            underlying = lp.get("model", "")
            api_base = lp.get("api_base")
            result["deployments"].append({
                "model_name": name,
                "underlying": underlying,
                "api_base": api_base,
                "id": mi.get("id"),
                "type": _classify_backend(name, underlying, api_base or ""),
            })
    elif err:
        result["errors"].append("model/info: %s" % err)

    if with_health:
        ok, data, err = http_get_json(base + "/health", api_key, health_timeout)
        if ok and isinstance(data, dict):
            result["reachable"] = True
            result["health"] = data
        elif err:
            result["errors"].append(
                "health: %s (모델 많으면 --health-timeout 늘리기)" % err)

    ok, data, err = http_get_json(base + "/v1/models", api_key, timeout)
    if ok and isinstance(data, dict):
        result["reachable"] = True
        result["models"] = [m.get("id") for m in data.get("data", []) if m.get("id")]
    elif err:
        result["errors"].append("v1/models: %s" % err)

    return result


def collect_backend(backend, timeout):
    """개별 vLLM/SGLang 백엔드 직접 probe: /v1/models + /health."""
    name = backend.get("name") or backend.get("url")
    base = (backend.get("url") or "").rstrip("/")
    api_key = backend.get("api_key")
    btype = backend.get("type", "vllm")
    out = {"name": name, "url": base, "type": btype, "up": False,
           "models": [], "error": None}

    ok, data, err = http_get_json(base + "/v1/models", api_key, timeout)
    if ok and isinstance(data, dict):
        out["up"] = True
        out["models"] = [m.get("id") for m in data.get("data", []) if m.get("id")]
    else:
        out["error"] = err

    if not out["up"]:  # /v1/models 실패 시 /health 라도 확인
        ok, _, _ = http_get_json(base + "/health", api_key, timeout)
        out["up"] = ok

    return out


def _strip_openai_suffix(api_base):
    """api_base 에 붙은 OpenAI 경로(/v1, /openai/v1)를 떼어 베이스만 남긴다."""
    base = api_base.rstrip("/")
    for suffix in ("/openai/v1", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def discover_backends(litellm_result):
    """LiteLLM 에서 받은 api_base 로 probe 대상 백엔드를 자동 발견.

    -> backends 를 수동으로 적을 필요가 없다. 주소의 원천은 LiteLLM 설정의
       litellm_params.api_base 이며, /model/info(우선) 또는 /health 가 그대로 돌려준다.
    """
    discovered = {}
    # 1순위: /model/info 의 deployments (api_base 평문 + 종류 분류 포함)
    for d in litellm_result.get("deployments") or []:
        api_base = d.get("api_base")
        if not api_base:
            continue
        base = _strip_openai_suffix(api_base)
        discovered.setdefault(base, {
            "name": d.get("model_name") or base,
            "url": base,
            "type": d.get("type", "-"),
        })
    # 2순위 보강: /health 에만 있는 주소
    health = litellm_result.get("health") or {}
    for ep in (health.get("healthy_endpoints") or []) + (
            health.get("unhealthy_endpoints") or []):
        api_base = ep.get("api_base")
        if not api_base:
            continue
        base = _strip_openai_suffix(api_base)
        if base in discovered:
            continue
        model = ep.get("model", "")
        btype = _classify_backend(model, model, base)
        discovered[base] = {"name": model or base, "url": base, "type": btype}
    return list(discovered.values())


# ----------------------------------------------------------------------------
# 사용량: 모델별 요청 수 / 토큰 / 사용률
#   LiteLLM 은 버전마다 분석(analytics) 엔드포인트가 달라진다(신설/폐기 반복).
#   그래서 후보 엔드포인트를 우선순위로 시도하고, 처음으로 데이터가 나온 응답만
#   정규화해서 쓴다. 전부 실패하면 usage 는 비어 있고 UI 는 '?' 로 표시한다
#   — backend 개수와 같은 원칙으로 값을 지어내지 않는다.
# ----------------------------------------------------------------------------

# 같은 의미인데 버전마다 키 이름이 다른 필드들
_USAGE_REQ_KEYS = ("api_requests", "total_requests", "sum_api_requests",
                   "num_requests", "requests", "successful_requests")
_USAGE_TOK_KEYS = ("total_tokens", "sum_total_tokens", "tokens")
_USAGE_SPEND_KEYS = ("spend", "total_spend", "sum_spend")


def _num(v):
    """숫자로 해석되면 float, 아니면 None (bool 은 숫자로 보지 않는다)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pick_num(d, keys):
    """dict 에서 후보 키들 중 처음으로 숫자인 값을 반환."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            n = _num(d.get(k))
            if n is not None:
                return n
    return None


def _usage_acc(acc, name, requests=None, tokens=None, spend=None):
    """모델별 누적기. 값이 없는(None) 항목은 건드리지 않는다."""
    if not name:
        return
    row = acc.setdefault(str(name), {"requests": None, "tokens": None, "spend": None})
    for key, val in (("requests", requests), ("tokens", tokens), ("spend", spend)):
        if val is None:
            continue
        row[key] = (row[key] or 0) + val


def _usage_from_activity_model(data):
    """GET /global/activity/model -> [{model, total_requests, total_tokens, daily_data[]}]"""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    acc = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("model") or row.get("model_group")
        if not name:
            continue
        req = _pick_num(row, _USAGE_REQ_KEYS)
        tok = _pick_num(row, _USAGE_TOK_KEYS)
        spend = _pick_num(row, _USAGE_SPEND_KEYS)
        # 상단 합계가 없는 버전은 daily_data 를 직접 합산
        if req is None and tok is None:
            req_d = tok_d = None
            for day in row.get("daily_data") or []:
                if not isinstance(day, dict):
                    continue
                dr, dt = _pick_num(day, _USAGE_REQ_KEYS), _pick_num(day, _USAGE_TOK_KEYS)
                if dr is not None:
                    req_d = (req_d or 0) + dr
                if dt is not None:
                    tok_d = (tok_d or 0) + dt
            req, tok = req if req is not None else req_d, tok if tok is not None else tok_d
        _usage_acc(acc, name, req, tok, spend)
    return acc


def _usage_from_daily_activity(data):
    """GET /global/daily/activity (신형) -> results[].breakdown.models{name: {metrics}}"""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return {}
    acc = {}
    for day in results:
        models = ((day or {}).get("breakdown") or {}).get("models") or {}
        if not isinstance(models, dict):
            continue
        for name, entry in models.items():
            metrics = entry.get("metrics") if isinstance(entry, dict) else None
            if not isinstance(metrics, dict):
                metrics = entry if isinstance(entry, dict) else {}
            _usage_acc(acc, name,
                       _pick_num(metrics, _USAGE_REQ_KEYS),
                       _pick_num(metrics, _USAGE_TOK_KEYS),
                       _pick_num(metrics, _USAGE_SPEND_KEYS))
    return acc


def _usage_from_model_metrics(data):
    """GET /model/metrics -> [{model, num_requests, ...}] (요청 수만 있는 경우가 많다)"""
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    acc = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("model") or row.get("model_group") or row.get("model_name")
        req = _pick_num(row, _USAGE_REQ_KEYS)
        if not name or req is None:
            continue
        _usage_acc(acc, name, req, _pick_num(row, _USAGE_TOK_KEYS),
                   _pick_num(row, _USAGE_SPEND_KEYS))
    return acc


def usage_candidates(now, window_hours):
    """(path, parser, granularity) 후보 목록 — 앞에 있는 것부터 시도한다.

    1순위 /global/activity/model 이 LiteLLM 소스(spend_management_endpoints.py)에서
    확인된 정식 경로다: LiteLLM_SpendLogs 를 model_group·일자로 GROUP BY 해서
    [{model, sum_api_requests, sum_total_tokens, daily_data[{date, api_requests,
    total_tokens}]}] 를 돌려준다. 2순위는 신형 일별 집계(/gateway/daily/activity),
    3순위는 구버전 UI 가 쓰던 /model/metrics.

    ※ /user/daily/activity 는 후보에 넣지 않는다 — 호출한 키의 사용자 범위만
      돌려줘서 전체 현황처럼 보여주면 조용히 과소 집계가 된다.

    granularity="day" 인 소스는 날짜 단위라 실제로 커버하는 구간이 요청한
    window 보다 넓다(그 날 00:00 부터). rate 계산 시 이를 반영한다.
    """
    start = now - timedelta(hours=window_hours)
    d0, d1 = start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    t0, t1 = start.strftime("%Y-%m-%dT%H:%M:%S"), now.strftime("%Y-%m-%dT%H:%M:%S")
    return [
        ("/global/activity/model?start_date=%s&end_date=%s" % (d0, d1),
         _usage_from_activity_model, "day"),
        ("/gateway/daily/activity?start_date=%s&end_date=%s&page_size=1000" % (d0, d1),
         _usage_from_daily_activity, "day"),
        ("/model/metrics?startTime=%s&endTime=%s" % (t0, t1),
         _usage_from_model_metrics, "exact"),
    ]


def collect_usage(url, api_key, timeout, window_hours=24.0, now=None):
    """LiteLLM 분석 엔드포인트에서 모델별 요청 수/토큰/비용 수집 -> 정규화 dict.

    반환: {"source": 성공한 엔드포인트|None, "window_hours", "window_minutes",
           "models": {model_name: {requests, tokens, spend, requests_per_min,
                                   tokens_per_min}},
           "totals": {...}, "errors": [...]}
    """
    now = now or datetime.now()
    start = now - timedelta(hours=window_hours)
    base = url.rstrip("/")
    # 사용량은 LiteLLM 이 요청을 DB(LiteLLM_SpendLogs)에 적어둬야 나온다.
    # DB 미연결이면 LiteLLM 이 "Database not connected" 를 돌려주고, 그 본문이
    # errors 에 그대로 실려 UI 에 노출된다(우리가 원인을 추측하지 않는다).
    out = {
        "source": None,
        "granularity": None,
        "window_hours": window_hours,
        "window_minutes": None,
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": now.strftime("%Y-%m-%d %H:%M:%S"),
        "models": {},
        "totals": {"requests": 0, "tokens": 0, "spend": 0.0,
                   "requests_per_min": 0.0, "models_used": 0},
        "errors": [],
    }

    models = {}
    for path, parser, granularity in usage_candidates(now, window_hours):
        ok, data, err = http_get_json(base + path, api_key, timeout)
        if not ok:
            out["errors"].append("%s: %s" % (path.split("?")[0], err))
            continue
        try:
            parsed = parser(data)
        except Exception as e:  # noqa: BLE001  (응답 형태가 예상 밖이어도 계속)
            out["errors"].append("%s: parse %s: %s"
                                 % (path.split("?")[0], type(e).__name__, e))
            continue
        if parsed:
            models = parsed
            out["source"] = path.split("?")[0]
            out["granularity"] = granularity
            break
        out["errors"].append("%s: 데이터 없음" % path.split("?")[0])

    # rate 기준 구간: 날짜 단위 소스는 그 날 00:00 부터 지금까지가 실제 커버 구간
    if out["granularity"] == "day":
        covered_start = datetime(start.year, start.month, start.day)
    else:
        covered_start = start
    minutes = max(1.0, (now - covered_start).total_seconds() / 60.0)
    out["window_minutes"] = round(minutes, 1)

    for name, row in models.items():
        req, tok = row.get("requests"), row.get("tokens")
        row["requests_per_min"] = round(req / minutes, 3) if req is not None else None
        row["tokens_per_min"] = round(tok / minutes, 1) if tok is not None else None
        for key in ("requests", "tokens"):
            if row.get(key) is not None:
                row[key] = int(row[key])
        out["totals"]["requests"] += row.get("requests") or 0
        out["totals"]["tokens"] += row.get("tokens") or 0
        out["totals"]["spend"] += row.get("spend") or 0.0
        if (row.get("requests") or 0) > 0:
            out["totals"]["models_used"] += 1
    out["models"] = models
    out["totals"]["requests_per_min"] = round(out["totals"]["requests"] / minutes, 3)
    out["totals"]["spend"] = round(out["totals"]["spend"], 6)
    return out


def _usage_key(s):
    return str(s or "").strip().lower()


def attach_usage_to_deployments(ll, usage):
    """usage(model_name 단위)를 deployment 행에 join + 한도 대비 사용률 계산.

    주의: 사용량은 **model_name(그룹) 단위**라 같은 이름의 deployment 가 여러 개면
    같은 값이 붙는다(행별로 쪼갤 근거가 없다). 합계는 반드시 usage["totals"] 를 쓴다.
    """
    deps = ll.get("deployments") or []
    models = (usage or {}).get("models") or {}
    if not models:
        return deps

    idx = {}
    for name, row in models.items():
        idx.setdefault(_usage_key(name), row)
        if "/" in str(name):   # provider prefix 제거본도 색인 (openai/Qwen -> qwen)
            idx.setdefault(_usage_key(str(name).split("/")[-1]), row)

    # /model_group/info 의 rpm/tpm 한도가 있으면 "사용률"의 분모로 쓴다.
    limits = {}
    for g in ll.get("groups") or []:
        rpm, tpm = _num(g.get("rpm")), _num(g.get("tpm"))
        if rpm or tpm:
            limits[_usage_key(g.get("model_group"))] = (rpm, tpm)

    out = []
    for d in deps:
        under = str(d.get("underlying") or "")
        row = None
        for cand in (d.get("model_name"), under, under.split("/")[-1]):
            if cand and _usage_key(cand) in idx:
                row = idx[_usage_key(cand)]
                break
        if row is None:
            out.append(d)
            continue
        u = dict(row)
        rpm_lim, tpm_lim = limits.get(_usage_key(d.get("model_name")), (None, None))
        if rpm_lim:
            u["rpm_limit"] = rpm_lim
            if u.get("requests_per_min") is not None:
                u["rpm_util"] = round(u["requests_per_min"] / rpm_lim, 4)
        if tpm_lim:
            u["tpm_limit"] = tpm_lim
            if u.get("tokens_per_min") is not None:
                u["tpm_util"] = round(u["tokens_per_min"] / tpm_lim, 4)
        out.append(dict(d, usage=u))
    return out


# ----------------------------------------------------------------------------
# 현재 부하(live): 백엔드 Prometheus /metrics 에서 실행/대기 요청 + KV 캐시 사용률
#   LiteLLM 집계는 "지난 N시간 누적"이라 지금 얼마나 물려 있는지는 알 수 없다.
#   그건 vLLM/SGLang 이 직접 노출하는 게이지에서만 나온다.
#   주의: api_base 는 LB 라서 /metrics 응답은 **뒤에 있는 Pod 중 하나**의 값이다.
# ----------------------------------------------------------------------------

_PROM_SPECS = [
    ("vllm", {
        "running": ("vllm:num_requests_running",),
        "waiting": ("vllm:num_requests_waiting",),
        "kv_cache": ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"),
    }),
    ("sglang", {
        "running": ("sglang:num_running_reqs",),
        "waiting": ("sglang:num_queue_reqs",),
        "kv_cache": ("sglang:token_usage", "sglang:kv_cache_usage",),
    }),
]


def parse_prom_metrics(text):
    """Prometheus 텍스트 -> {metric_name: [값, ...]} (라벨은 무시, 값만 모은다)."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name = line.split("{", 1)[0].strip()
            rest = line.rsplit("}", 1)[-1].split()
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, rest = parts[0], parts[1:]
        if not rest:
            continue
        val = _num(rest[0])
        if val is None:
            continue
        out.setdefault(name, []).append(val)
    return out


def live_from_prom(text):
    """/metrics 본문 -> {"engine","running","waiting","kv_cache_pct"} (모르면 None)."""
    metrics = parse_prom_metrics(text)
    if not metrics:
        return None
    for engine, spec in _PROM_SPECS:
        if not any(n in metrics for names in spec.values() for n in names):
            continue
        live = {"engine": engine, "running": None, "waiting": None,
                "kv_cache_pct": None}
        for field, names in spec.items():
            vals = []
            for n in names:
                vals.extend(metrics.get(n) or [])
            if not vals:
                continue
            if field == "kv_cache":
                # 0~1 비율로 나오는 게 보통 -> % 로. 여러 라벨이면 최댓값(가장 붐비는 쪽).
                pct = max(vals)
                live["kv_cache_pct"] = round(pct * 100.0 if pct <= 1.0 else pct, 1)
            else:
                live[field] = int(sum(vals))   # 라벨(모델)별로 나뉘어 있으면 합
        return live
    return None


def collect_live_metrics(bases, timeout, api_key=None, max_threads=8):
    """백엔드 base URL 목록 -> {base: live dict}. 스레드로 병렬 probe."""
    bases = [b for b in dict.fromkeys(bases) if b]
    out, lock = {}, threading.Lock()

    def work(queue):
        while True:
            with lock:
                if not queue:
                    return
                base = queue.pop()
            ok, text, err = http_get_text(base + "/metrics", api_key, timeout,
                                          accept="text/plain")
            live = live_from_prom(text) if ok else None
            if live is None:
                live = {"engine": None, "running": None, "waiting": None,
                        "kv_cache_pct": None,
                        "error": err or "metrics 파싱 실패(엔진 게이지 없음)"}
            live["url"] = base + "/metrics"
            with lock:
                out[base] = live

    queue = list(bases)
    threads = [threading.Thread(target=work, args=(queue,), daemon=True)
               for _ in range(min(max_threads, len(queue)) or 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def attach_live_to_deployments(deployments, live_by_base):
    """base URL 기준으로 live 지표를 deployment 행에 붙인다."""
    out = []
    for d in deployments or []:
        base = _strip_openai_suffix(d["api_base"]) if d.get("api_base") else None
        live = live_by_base.get(base) if base else None
        out.append(dict(d, live=live) if live else d)
    return out

# ----------------------------------------------------------------------------
# Backend 개수: api_base(LB) 뒤의 실제 Pod/replica 수
#   1순위 EndpointSlice(ready 주소 수) -> Serverless 면 Knative PodAutoscaler
#   -> RawDeployment Deployment -> none
#   외부 패키지 0개: urllib + ssl(표준 라이브러리)로 in-cluster k8s API 호출
# ----------------------------------------------------------------------------

import ssl  # noqa: E402  (표준 라이브러리)
import urllib.parse  # noqa: E402


class K8sClient:
    """in-cluster Kubernetes API 를 표준 라이브러리만으로 호출."""

    def __init__(self, api_server, token, ssl_ctx, timeout, default_namespace):
        self.api_server = api_server.rstrip("/") if api_server else None
        self.token = token
        self.ssl_ctx = ssl_ctx
        self.timeout = timeout
        self.default_namespace = default_namespace or "default"

    @property
    def enabled(self):
        return bool(self.api_server)

    @classmethod
    def from_settings(cls, settings):
        """in-cluster ServiceAccount 토큰/CA 가 있으면 활성, 없으면 None."""
        if not settings.get("backend_count"):
            return None

        # API server 주소: 명시 > env > 기본 in-cluster DNS
        api_server = settings.get("k8s_api_server")
        if not api_server:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if host:
                api_server = "https://%s:%s" % (host, port)

        token = None
        token_file = settings.get("k8s_token_file")
        if token_file and os.path.exists(token_file):
            try:
                with open(token_file) as f:
                    token = f.read().strip()
            except OSError:
                token = None

        # 토큰이 없으면 클러스터 밖(개발환경)으로 보고 k8s API 비활성
        if not token:
            api_server = None

        ssl_ctx = None
        if api_server:
            if settings.get("k8s_insecure"):
                ssl_ctx = ssl._create_unverified_context()
            else:
                ca = settings.get("k8s_ca_file")
                try:
                    if ca and os.path.exists(ca):
                        ssl_ctx = ssl.create_default_context(cafile=ca)
                    else:
                        ssl_ctx = ssl.create_default_context()
                except Exception:
                    ssl_ctx = ssl._create_unverified_context()

        # 네임스페이스 폴백 최종값: 설정 > SA namespace 파일 > default
        ns = settings.get("default_namespace")
        if not ns:
            ns_file = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
            if os.path.exists(ns_file):
                try:
                    with open(ns_file) as f:
                        ns = f.read().strip()
                except OSError:
                    ns = None

        if not api_server:   # 토큰/주소 없으면(클러스터 밖) backend 개수 수집 불가
            return None
        return cls(
            api_server=api_server, token=token, ssl_ctx=ssl_ctx,
            timeout=settings.get("k8s_timeout", 5.0), default_namespace=ns,
        )

    def get(self, path):
        """k8s API GET -> (ok, data, err)."""
        if not self.api_server:
            return False, None, "k8s api server not configured"
        url = self.api_server + path
        headers = {"Accept": "application/json",
                   "Authorization": "Bearer %s" % self.token}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                return True, json.loads(resp.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            return False, None, "HTTP %s %s" % (e.code, e.reason)
        except Exception as e:  # noqa: BLE001
            return False, None, "%s: %s" % (type(e).__name__, e)


def parse_api_base(api_base, default_namespace="default", overrides=None):
    """api_base URL -> {service, namespace, port, kind}.

    예: http://qwen36-35b-predictor.kserve.svc:8080/v1
        -> service=qwen36-35b-predictor, namespace=kserve, port=8080, kind=k8s-svc
    클러스터 Service 가 아니면 kind='external'.
    """
    overrides = overrides or {}
    try:
        parts = urllib.parse.urlsplit(api_base)
    except Exception:
        return {"service": None, "namespace": None, "port": None, "kind": "external"}
    host = parts.hostname or ""
    port = parts.port
    if not host:
        return {"service": None, "namespace": None, "port": port, "kind": "external"}

    if host in overrides:  # host 통째 override 우선
        return {"service": host.split(".")[0],
                "namespace": overrides[host], "port": port, "kind": "k8s-svc"}

    host = host.rstrip(".")            # FQDN 절대표기 'svc.cluster.local.' 처리
    tokens = host.split(".")

    # IP 주소거나 공인 도메인(.com/.net 등)이면 클러스터 Service 아님
    if all(t.isdigit() for t in tokens) or ":" in host:
        return {"service": None, "namespace": None, "port": port, "kind": "external"}

    service = tokens[0]
    namespace = None
    if "svc" in tokens:
        # [svc].[ns].svc[.cluster.local]
        idx = tokens.index("svc")
        if idx >= 2:
            namespace = tokens[idx - 1]
    elif len(tokens) >= 2 and tokens[-1] not in (
            "com", "net", "org", "io", "ai", "co", "dev", "local"):
        # [svc].[ns] 단축형 (위험 — overrides 권장)
        namespace = tokens[1]
    elif len(tokens) >= 3:
        # 외부 도메인으로 판단
        return {"service": None, "namespace": None, "port": port, "kind": "external"}

    # service 이름 override (ns 추론 불가한 단축형 보강)
    if service in overrides:
        namespace = overrides[service]
    if not namespace:
        namespace = default_namespace
    return {"service": service, "namespace": namespace, "port": port,
            "kind": "k8s-svc"}


def _is_activator(ep, activator_ns):
    ref = ep.get("targetRef") or {}
    if ref.get("namespace") == activator_ns:
        return True
    name = (ref.get("name") or "").lower()
    return name.startswith("activator")


def count_via_endpointslice(client, ns, svc, activator_ns):
    """EndpointSlice ready 주소 수 합산. activator 만 있으면 activator_only=True."""
    path = ("/apis/discovery.k8s.io/v1/namespaces/%s/endpointslices"
            "?labelSelector=kubernetes.io/service-name=%s" % (ns, svc))
    ok, data, err = client.get(path)
    if not ok:
        return None, err
    ready = 0
    saw_activator = False
    saw_real = False
    for item in data.get("items", []) or []:
        for ep in item.get("endpoints", []) or []:
            cond = ep.get("conditions") or {}
            if cond.get("ready") is False:
                continue
            addrs = ep.get("addresses") or []
            if _is_activator(ep, activator_ns):
                saw_activator = True
                continue
            saw_real = True
            ready += len(addrs)
    return {"ready": ready, "activator_only": saw_activator and not saw_real}, None


def count_via_endpoints(client, ns, svc):
    """core/v1 Endpoints 폴백(구버전 k8s): subsets addresses 고유 IP 수."""
    ok, data, err = client.get(
        "/api/v1/namespaces/%s/endpoints/%s" % (ns, svc))
    if not ok:
        return None, err
    ips = set()
    for sub in data.get("subsets", []) or []:
        for a in sub.get("addresses", []) or []:
            if a.get("ip"):
                ips.add(a["ip"])
    return {"ready": len(ips)}, None


def detect_mode_and_revision(client, ns, svc):
    """service 이름에서 ISVC 추정 -> deploymentMode + revision + found 여부."""
    isvc = svc
    for suffix in ("-predictor", "-transformer", "-explainer"):
        if isvc.endswith(suffix):
            isvc = isvc[: -len(suffix)]
            break
    ok, data, err = client.get(
        "/apis/serving.kserve.io/v1beta1/namespaces/%s/inferenceservices/%s"
        % (ns, isvc))
    if not ok:
        return {"mode": "Unknown", "revision": None, "isvc": isvc,
                "found": False}, err
    status = data.get("status") or {}
    mode = status.get("deploymentMode") or (
        data.get("metadata", {}).get("annotations", {})
        .get("serving.kserve.io/deploymentMode")) or "Unknown"
    pred = (status.get("components") or {}).get("predictor") or {}
    # revision 필드는 KServe/Knative 버전마다 달라 여러 후보를 순차 시도
    rev = (pred.get("latestReadyRevision")
           or pred.get("latestCreatedRevision")
           or pred.get("latestRolledoutRevision"))
    if not rev:
        for t in (pred.get("traffic") or []):
            if t.get("revisionName"):
                rev = t["revisionName"]
                break
    return {"mode": mode, "revision": rev, "isvc": isvc, "found": True}, None


def _is_serverless(mode, revision):
    """Knative/Serverless 여부. revision 이 있으면 Knative-backed 으로 본다."""
    if revision:
        return True
    m = (mode or "").lower()
    return ("serverless" in m) or ("knative" in m)


def count_via_deployment_label(client, ns, isvc):
    """KServe Deployment 들을 라벨로 합산 — raw/serverless 공통, revision 이름 불필요.

    KServe 가 predictor Deployment 에 serving.kserve.io/inferenceservice=<isvc>
    라벨을 붙이므로, Knative 네이밍/activator 를 몰라도 ready Pod 수를 합산할 수 있다.
    """
    sel = "serving.kserve.io/inferenceservice=%s" % isvc
    ok, data, err = client.get(
        "/apis/apps/v1/namespaces/%s/deployments?labelSelector=%s" % (ns, sel))
    if not ok:
        return None, "deployments(label): %s" % err
    items = data.get("items") or []
    if not items:
        return None, "deployments(label): no match (%s)" % sel
    ready = sum(int((d.get("status") or {}).get("readyReplicas") or 0)
                for d in items)
    desired = sum(int((d.get("spec") or {}).get("replicas") or 0)
                  for d in items)
    return {"ready": ready, "desired": desired, "source": "deployment"}, None


def count_via_knative(client, ns, revision):
    """Knative PodAutoscaler actualScale(실시간) 우선, 실패 시 Revision."""
    if not revision:
        return None, "no revision"
    ok, data, err = client.get(
        "/apis/autoscaling.internal.knative.dev/v1alpha1/namespaces/%s/podautoscalers/%s"
        % (ns, revision))
    if ok:
        st = data.get("status") or {}
        actual = st.get("actualScale")
        desired = st.get("desiredScale")
        if actual is not None:
            return {"ready": int(actual), "desired": _int_or_none(desired),
                    "source": "knative-pa",
                    "scale_to_zero": int(actual) == 0}, None
    ok2, data2, err2 = client.get(
        "/apis/serving.knative.dev/v1/namespaces/%s/revisions/%s" % (ns, revision))
    if ok2:
        st = data2.get("status") or {}
        actual = st.get("actualReplicas")
        if actual is not None:
            return {"ready": int(actual),
                    "desired": _int_or_none(st.get("desiredReplicas")),
                    "source": "knative-revision",
                    "scale_to_zero": int(actual) == 0}, None
        return None, "pa/revision 에 actualScale 필드 없음"
    return None, (err or err2 or "knative 조회 실패")


def count_via_deployment(client, ns, svc):
    """RawDeployment 보정: Deployment status.readyReplicas / spec.replicas."""
    dep = svc  # KServe RawDeployment 는 보통 service 이름과 동일({isvc}-predictor)
    ok, data, err = client.get(
        "/apis/apps/v1/namespaces/%s/deployments/%s" % (ns, dep))
    if not ok:
        return None, err
    spec = data.get("spec") or {}
    st = data.get("status") or {}
    return {"ready": int(st.get("readyReplicas") or 0),
            "desired": _int_or_none(spec.get("replicas")),
            "source": "deployment"}, None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def resolve_backend_count(deployment, client, settings, cache=None):
    """우선순위 체인으로 한 deployment 의 LB 뒤 backend 개수 산출 -> 필드 dict.

    cache={(ns,svc): out} 를 주면 같은 Service 를 가리키는 여러 model_name 이
    한 스냅샷 빌드 안에서 k8s API 를 중복 조회하지 않고 결과를 재사용한다.
    """
    out = {"backends_ready": None, "backends_desired": None,
           "backend_source": "none", "mode": "Unknown",
           "scale_to_zero": False, "namespace": None, "service": None,
           "k8s_error": None}
    api_base = deployment.get("api_base")
    if not api_base:
        return out

    parsed = parse_api_base(api_base, client.default_namespace,
                            settings.get("namespace_overrides"))
    out["namespace"] = parsed["namespace"]
    out["service"] = parsed["service"]
    if parsed["kind"] != "k8s-svc" or not parsed["service"]:
        out["backend_source"] = "external"
        return out

    if not client.enabled:
        return out

    ns, svc = parsed["namespace"], parsed["service"]
    if cache is not None and (ns, svc) in cache:
        return dict(cache[(ns, svc)])   # 같은 Service 는 재조회 생략
    activator_ns = settings.get("activator_namespace", "knative-serving")
    errors = []

    info, _ = detect_mode_and_revision(client, ns, svc)
    out["mode"] = info["mode"]
    isvc, revision = info["isvc"], info["revision"]
    serverless = _is_serverless(info["mode"], revision)

    def setres(r):
        out["backends_ready"] = r["ready"]
        if r.get("desired") is not None:
            out["backends_desired"] = r["desired"]
        out["backend_source"] = r["source"]
        if r.get("scale_to_zero"):
            out["scale_to_zero"] = True

    if info["found"]:
        # --- KServe ISVC: Deployment 라벨 합산이 raw/serverless 공통으로 가장 견고 ---
        dep, err = count_via_deployment_label(client, ns, isvc)
        if dep is not None:
            setres(dep)
        else:
            errors.append(err)
            # serverless 면 Knative PodAutoscaler/Revision 으로 보강
            if revision:
                kn, kerr = count_via_knative(client, ns, revision)
                if kn is not None:
                    setres(kn)
                else:
                    errors.append("knative: %s" % kerr)
            else:
                errors.append("no revision (ISVC status 에 revision 없음)")
        # serverless 이고 0 이면 scale-to-zero 로 표기(장애 아님)
        if serverless and out["backends_ready"] == 0:
            out["scale_to_zero"] = True
    else:
        # --- 일반 Service(비 KServe): EndpointSlice 의 ready 주소 수 ---
        es, err = count_via_endpointslice(client, ns, svc, activator_ns)
        if es is not None and not es.get("activator_only"):
            out["backends_ready"] = es["ready"]
            out["backend_source"] = "endpointslice"
        elif es is not None and es.get("activator_only"):
            errors.append("endpointslice: activator only (serverless?)")
        elif es is None and err and "404" in err:
            eps, eerr = count_via_endpoints(client, ns, svc)
            if eps is not None:
                out["backends_ready"] = eps["ready"]
                out["backend_source"] = "endpoints"
            else:
                errors.append("endpoints: %s" % eerr)
        else:
            errors.append("endpointslice: %s" % err)
        # desired 보강(같은 이름 Deployment), ready 미상이면 그걸로 대체
        dep, derr = count_via_deployment(client, ns, svc)
        if dep is not None:
            if out["backends_desired"] is None:
                out["backends_desired"] = dep.get("desired")
            if out["backends_ready"] is None:
                out["backends_ready"] = dep["ready"]
                out["backend_source"] = "deployment"

    if out["backends_ready"] is None and errors:
        out["k8s_error"] = "; ".join(errors)
    if cache is not None:
        cache[(ns, svc)] = dict(out)
    return out


def build_snapshot(settings, with_health=True):
    """전체 수집 -> 스냅샷 dict.

    with_health=False 면 느린 /health 를 건너뛴다(웹은 health 를 별도 스레드로 주입).
    """
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "litellm": None,
        "backends": [],
        "usage": None,
        "summary": {},
    }

    if settings["litellm_url"]:
        snap["litellm"] = collect_litellm(
            settings["litellm_url"], settings["api_key"], settings["timeout"],
            settings.get("health_timeout"), with_health=with_health
        )

    if settings["probe_backends"]:
        targets = settings["backends"]
        # 수동 목록이 없으면 LiteLLM /model/info + /health 에서 자동 발견.
        if not targets and snap["litellm"]:
            targets = discover_backends(snap["litellm"])
        for b in targets:
            snap["backends"].append(collect_backend(b, settings["timeout"]))

    # 각 deployment 의 LB(api_base) 뒤 backend Pod 개수 채우기
    client = K8sClient.from_settings(settings)
    snap["backend_count_enabled"] = bool(client)
    if client and snap["litellm"]:
        bc_cache = {}  # (ns,svc) -> 결과: 같은 Service 중복 k8s 조회 방지
        for d in snap["litellm"].get("deployments") or []:
            try:
                d.update(resolve_backend_count(d, client, settings, bc_cache))
            except Exception as e:  # noqa: BLE001  (한 건 실패가 전체를 막지 않게)
                d["k8s_error"] = "%s: %s" % (type(e).__name__, e)

    # /health 상태 + backend readiness 를 합쳐 deployment 에 status 부여
    # (TUI/웹/JSON 모두 동일한 status 를 쓰도록 여기서 한 번에 적용)
    if snap["litellm"]:
        snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])

    # 모델별 사용량(요청 수/토큰/사용률) — LiteLLM 분석 엔드포인트
    if settings.get("usage") and settings.get("litellm_url") and snap["litellm"]:
        try:
            snap["usage"] = collect_usage(
                settings["litellm_url"], settings["api_key"], settings["timeout"],
                settings.get("usage_window", 24.0))
            snap["litellm"]["deployments"] = attach_usage_to_deployments(
                snap["litellm"], snap["usage"])
        except Exception as e:  # noqa: BLE001  (사용량 실패가 전체를 막지 않게)
            snap["usage"] = {"models": {}, "totals": {}, "source": None,
                             "errors": ["%s: %s" % (type(e).__name__, e)]}

    # 현재 부하(live): 백엔드 /metrics 직접 probe (엔진 게이지)
    if settings.get("probe_metrics") and snap["litellm"]:
        deps = snap["litellm"].get("deployments") or []
        bases = [_strip_openai_suffix(d["api_base"]) for d in deps if d.get("api_base")]
        try:
            live = collect_live_metrics(bases, settings["timeout"],
                                        max_threads=settings.get("metrics_threads", 8))
            snap["live_metrics_enabled"] = True
            snap["litellm"]["deployments"] = attach_live_to_deployments(deps, live)
        except Exception as e:  # noqa: BLE001
            snap["live_metrics_error"] = "%s: %s" % (type(e).__name__, e)

    snap["summary"] = summarize(snap)
    return snap


def merge_deployments_with_health(ll):
    """/model/info(api_base) 와 /health(상태)를 api_base 기준으로 합친 뷰."""
    health = ll.get("health") or {}
    healthy = {_strip_openai_suffix(ep["api_base"])
               for ep in (health.get("healthy_endpoints") or [])
               if ep.get("api_base")}
    unhealthy = {_strip_openai_suffix(ep["api_base"])
                 for ep in (health.get("unhealthy_endpoints") or [])
                 if ep.get("api_base")}
    merged = []
    for d in ll.get("deployments") or []:
        base = _strip_openai_suffix(d["api_base"]) if d.get("api_base") else None
        if base in healthy:
            status, src = "UP", "health"
        elif base in unhealthy:
            status, src = "DOWN", "health"
        else:
            # /health 미조회(타임아웃/권한)거나 매칭 실패 -> backend readiness 로 추정
            r = d.get("backends_ready")
            if r is not None and d.get("backend_source") != "external":
                if r > 0:
                    status, src = "UP", "k8s"
                elif d.get("scale_to_zero"):
                    status, src = "?", "k8s"      # scale-to-zero = 정상 idle
                else:
                    status, src = "DOWN", "k8s"
            else:
                status, src = "?", "unknown"
        merged.append({**d, "status": status, "status_source": src})
    # LiteLLM 은 replica 구성에 따라 model/info 순서가 매번 달라질 수 있어
    # model_name 기준으로 정렬해 표시 순서를 안정화한다(TUI/웹/JSON 공통).
    merged.sort(key=lambda x: str(x.get("model_name") or "").lower())
    return merged


def summarize(snap):
    """핵심 지표 계산: 모델 그룹 수, 등록 deployment, 떠 있는 모델(healthy) 수."""
    s = {
        "model_groups": 0,
        "deployments_registered": 0,
        "deployments_total": 0,
        "deployments_healthy": 0,
        "deployments_unhealthy": 0,
        "backends_up": 0,
        "backends_total": 0,
        "backend_models": 0,
        "backend_pods_ready": 0,     # 모든 LB 뒤 ready Pod 합계
        "backend_pods_desired": 0,   # 목표 replica 합계
        "backend_pods_known": False,
        # 사용량(LiteLLM 분석 엔드포인트, window 누적)
        "usage_known": False,
        "usage_requests": 0,
        "usage_tokens": 0,
        "usage_spend": 0.0,
        "usage_rpm": 0.0,
        "usage_models_used": 0,
        "usage_window_hours": None,
        # 현재 부하(백엔드 /metrics 게이지)
        "live_known": False,
        "live_running": 0,
        "live_waiting": 0,
    }
    ll = snap.get("litellm")
    if ll:
        s["model_groups"] = len(ll.get("groups") or [])
        deps = ll.get("deployments") or []
        s["deployments_registered"] = len(deps)
        health = ll.get("health") or {}

        # 카드 수치를 표(merge_deployments_with_health 의 per-row status)와 항상
        # 일치시킨다. deployment 가 있으면 merged status 로 집계(=/health 가
        # 타임아웃해도 k8s readiness 보정이 반영됨), 없으면 /health 카운트로 폴백.
        if deps and any("status" in d for d in deps):
            s["deployments_healthy"] = sum(1 for d in deps if d.get("status") == "UP")
            s["deployments_unhealthy"] = sum(1 for d in deps if d.get("status") == "DOWN")
            s["deployments_total"] = len(deps)
        else:
            hc = health.get("healthy_count")
            uc = health.get("unhealthy_count")
            if hc is None:
                hc = len(health.get("healthy_endpoints") or [])
            if uc is None:
                uc = len(health.get("unhealthy_endpoints") or [])
            s["deployments_healthy"] = hc
            s["deployments_unhealthy"] = uc
            s["deployments_total"] = hc + uc

        # LB 뒤 backend Pod 집계 (값이 있는 deployment 만)
        for d in ll.get("deployments") or []:
            if d.get("backends_ready") is not None:
                s["backend_pods_ready"] += d["backends_ready"]
                s["backend_pods_known"] = True
            if d.get("backends_desired") is not None:
                s["backend_pods_desired"] += d["backends_desired"]

    # 사용량 합계는 usage["totals"] 에서 온다. deployment 행을 더하면 안 된다
    # — 사용량은 model_name(그룹) 단위라 같은 이름의 replica 행에 같은 값이 붙는다.
    usage = snap.get("usage") or {}
    totals = usage.get("totals") or {}
    if usage.get("source"):
        s["usage_known"] = True
        s["usage_requests"] = int(totals.get("requests") or 0)
        s["usage_tokens"] = int(totals.get("tokens") or 0)
        s["usage_spend"] = round(float(totals.get("spend") or 0.0), 6)
        s["usage_rpm"] = totals.get("requests_per_min") or 0.0
        s["usage_models_used"] = int(totals.get("models_used") or 0)
        s["usage_window_hours"] = usage.get("window_hours")

    # 현재 부하: 같은 api_base(LB)를 공유하는 행이 있으면 한 번만 센다
    seen_live = set()
    for d in (ll.get("deployments") or []) if ll else []:
        live = d.get("live")
        if not live or live.get("error"):
            continue
        key = live.get("url") or d.get("api_base")
        if key in seen_live:
            continue
        seen_live.add(key)
        if live.get("running") is not None:
            s["live_running"] += live["running"]
            s["live_known"] = True
        if live.get("waiting") is not None:
            s["live_waiting"] += live["waiting"]
            s["live_known"] = True

    backends = snap.get("backends") or []
    s["backends_total"] = len(backends)
    s["backends_up"] = sum(1 for b in backends if b.get("up"))
    s["backend_models"] = sum(len(b.get("models") or []) for b in backends)
    return s


# ----------------------------------------------------------------------------
# Rendering (ANSI, no deps)
# ----------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, color):
    if not _USE_COLOR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36",
             "bold": "1", "dim": "2", "magenta": "35"}
    return "\033[%sm%s\033[0m" % (codes.get(color, "0"), text)


def _fmt_backends(d):
    """deployment 의 LB 뒤 backend 개수를 컬러 셀로 포맷."""
    ready = d.get("backends_ready")
    desired = d.get("backends_desired")
    src = d.get("backend_source", "none")

    if src == "external":
        return c("external", "dim")
    if d.get("scale_to_zero"):
        return c("0 (scaled-to-zero)", "yellow")
    if ready is None:
        # /health UP 인데 0/미상이면 activator 경유 가능성 힌트
        if d.get("status") == "UP" and src == "none":
            return c("? (via activator?)", "yellow")
        return c("?", "dim")

    if desired is None:
        body = "%d" % ready
    else:
        body = "%d/%d" % (ready, desired)

    if ready == 0:
        color = "red"
    elif desired is not None and ready < desired:
        color = "yellow"
    else:
        color = "green"
    return c(body, color)


def _fmt_num(n):
    """1234567 -> '1.2M' / 12840 -> '12.8k' (표 폭 절약)."""
    if n is None:
        return "?"
    n = float(n)
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return "%.1f%s" % (n / div, unit)
    return "%d" % int(n)


def _util_color(ratio):
    """사용률(0~1) -> 색. 한도에 가까울수록 경고."""
    if ratio is None:
        return "dim"
    if ratio >= 0.9:
        return "red"
    if ratio >= 0.7:
        return "yellow"
    return "green"


def _fmt_requests(d):
    """window 누적 요청 수 셀."""
    u = d.get("usage") or {}
    req = u.get("requests")
    if req is None:
        return c("?", "dim")
    return c(_fmt_num(req), "cyan" if req else "dim")


def _fmt_rate(d):
    """분당 요청(rpm) + 한도가 있으면 사용률 %."""
    u = d.get("usage") or {}
    rpm = u.get("requests_per_min")
    if rpm is None:
        return c("?", "dim")
    body = "%.2f/m" % rpm
    util = u.get("rpm_util")
    if util is None:
        return c(body, "dim" if not rpm else "cyan")
    return "%s %s" % (c(body, "dim" if not rpm else "cyan"),
                      c("(%.0f%%)" % (util * 100), _util_color(util)))


def _fmt_live(d):
    """현재 실행/대기 요청 (백엔드 /metrics 게이지). 'run+wait' 표기."""
    live = d.get("live")
    if not live:
        return c("-", "dim")
    if live.get("error"):
        return c("?", "dim")
    run, wait = live.get("running"), live.get("waiting")
    if run is None and wait is None:
        return c("?", "dim")
    body = "%s" % (run if run is not None else "?")
    if wait:
        return "%s %s" % (c(body, "green"), c("+%d wait" % wait, "yellow"))
    return c(body, "green" if run else "dim")


def _fmt_kv(d):
    """KV 캐시 사용률(%) — GPU 메모리가 얼마나 물려 있는지 = 진짜 '사용률'."""
    live = d.get("live") or {}
    pct = live.get("kv_cache_pct")
    if pct is None:
        return c("?", "dim") if live else c("-", "dim")
    return c("%.0f%%" % pct, _util_color(pct / 100.0))


def _table(headers, rows, aligns=None):
    """간단한 모노스페이스 테이블 렌더."""
    cols = len(headers)
    aligns = aligns or ["l"] * cols
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(_plain(str(row[i]))))

    def fmt(cells, bold=False):
        parts = []
        for i in range(cols):
            cell = str(cells[i])
            pad = widths[i] - len(_plain(cell))
            if aligns[i] == "r":
                parts.append(" " * pad + cell)
            else:
                parts.append(cell + " " * pad)
        line = "  ".join(parts)
        return c(line, "bold") if bold else line

    out = [fmt(headers, bold=True)]
    out.append(c("-" * (sum(widths) + 2 * (cols - 1)), "dim"))
    for row in rows:
        out.append(fmt(row))
    return "\n".join(out)


def _plain(s):
    """ANSI 코드 제거한 표시 길이 계산용."""
    res = []
    i = 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            res.append(s[i])
            i += 1
    return "".join(res)


def render(snap, settings):
    lines = []
    s = snap["summary"]
    ver = snap.get("version", __version__)
    lines.append(c("=== Model Monitor ===", "bold")
                 + c(" v%s" % ver, "cyan")
                 + c("  %s" % snap["ts"], "dim"))
    lines.append("")

    # 핵심 요약
    running = c(str(s["deployments_healthy"]), "green")
    unhealthy = c(str(s["deployments_unhealthy"]),
                  "red" if s["deployments_unhealthy"] else "dim")
    summary_bits = [
        "model groups: %s" % c(str(s["model_groups"]), "cyan"),
        "registered: %s" % c(str(s["deployments_registered"]), "cyan"),
        "running(healthy): %s" % running,
        "unhealthy: %s" % unhealthy,
    ]
    if s.get("backend_pods_known"):
        summary_bits.append(
            "backend pods: %s/%s" % (
                c(str(s["backend_pods_ready"]), "green"),
                s["backend_pods_desired"] or "?")
        )
    if s.get("usage_known"):
        win = s.get("usage_window_hours") or 24
        summary_bits.append(
            "requests(%gh): %s" % (win, c(_fmt_num(s["usage_requests"]), "cyan")))
        summary_bits.append("rpm: %s" % c("%.1f" % s["usage_rpm"], "cyan"))
    if s.get("live_known"):
        live_bit = c(str(s["live_running"]), "green")
        if s["live_waiting"]:
            live_bit += c(" +%d wait" % s["live_waiting"], "yellow")
        summary_bits.append("in-flight: %s" % live_bit)
    if settings["probe_backends"]:
        summary_bits.append(
            "backends up: %s/%s" % (
                c(str(s["backends_up"]), "green"), s["backends_total"])
        )
    lines.append("  " + "   ".join(summary_bits))
    lines.append("")

    ll = snap.get("litellm")
    if ll is None:
        lines.append(c("  (LiteLLM URL 미설정 — --litellm-url 또는 config 필요)", "yellow"))
    else:
        if not ll["reachable"]:
            lines.append(c("  [LiteLLM] 연결 실패: %s" % ll["url"], "red"))
            for e in ll["errors"]:
                lines.append(c("    - %s" % e, "red"))
        else:
            # 모델 그룹 테이블
            rows = []
            for g in ll["groups"]:
                providers = ",".join(g.get("providers") or []) or "-"
                rows.append([
                    g.get("model_group", "?"),
                    providers,
                    g.get("mode") or "-",
                ])
            if rows:
                lines.append(c("  [Model Groups] (LiteLLM /model_group/info)", "bold"))
                lines.append(indent(_table(
                    ["MODEL_GROUP", "PROVIDERS", "MODE"], rows), 2))
                lines.append("")

            # Deployments: model_name -> api_base (/model/info) + 상태(/health)
            #              + LB 뒤 backend Pod 개수
            merged = merge_deployments_with_health(ll)
            show_backends = snap.get("backend_count_enabled")
            usage = snap.get("usage") or {}
            show_usage = bool(usage.get("models"))
            show_live = bool(snap.get("live_metrics_enabled"))
            win = usage.get("window_hours") or 24
            if merged:
                drows = []
                for d in merged:
                    color = {"UP": "green", "DOWN": "red"}.get(d["status"], "yellow")
                    row = [
                        c(d["status"], color),
                        d.get("model_name", "?"),
                        d.get("type", "-"),
                    ]
                    if show_backends:
                        row.append(_fmt_backends(d))
                    if show_usage:
                        row.append(_fmt_requests(d))
                        row.append(_fmt_rate(d))
                    if show_live:
                        row.append(_fmt_live(d))
                        row.append(_fmt_kv(d))
                    if show_backends:
                        row.append(c(d.get("backend_source", "-"), "dim"))
                    row.append(d.get("api_base") or "-")
                    drows.append(row)
                hdr = ["STATUS", "MODEL_NAME", "TYPE"]
                if show_backends:
                    hdr.append("BACKENDS")
                if show_usage:
                    hdr += ["REQ/%gH" % win, "RPM"]
                if show_live:
                    hdr += ["IN-FLIGHT", "KV%"]
                if show_backends:
                    hdr.append("SRC")
                hdr.append("API_BASE")
                title = ("  [Deployments] (/model/info api_base + /health status"
                         + (" + LB backend pods" if show_backends else "")
                         + (" + usage" if show_usage else "")
                         + (" + live" if show_live else "") + ")")
                lines.append(c(title, "bold"))
                lines.append(indent(_table(hdr, drows), 2))
                lines.append("")
            else:
                # /model/info 가 없으면 /health 의 raw 엔드포인트라도 표시
                health = ll.get("health") or {}
                hrows = []
                for ep in health.get("healthy_endpoints") or []:
                    hrows.append([c("UP", "green"), ep.get("model", "?"),
                                  ep.get("api_base", "-")])
                for ep in health.get("unhealthy_endpoints") or []:
                    hrows.append([c("DOWN", "red"), ep.get("model", "?"),
                                  ep.get("api_base", ep.get("error", "-"))])
                if hrows:
                    lines.append(c("  [Deployments Health] (LiteLLM /health)", "bold"))
                    lines.append(indent(_table(
                        ["STATUS", "MODEL", "API_BASE"], hrows), 2))
                    lines.append("")
            if show_usage:
                lines.append(c("  usage 출처: %s · 집계구간 %s ~ %s (모델 이름 단위 합계)"
                               % (usage.get("source"), usage.get("start"),
                                  usage.get("end")), "dim"))
                lines.append("")
            elif snap.get("usage") and snap["usage"].get("errors"):
                lines.append(c("  ! 사용량 수집 실패(요청 수 열 생략): %s"
                               % "; ".join(snap["usage"]["errors"][:2]), "yellow"))
                lines.append("")
            if ll["errors"]:
                for e in ll["errors"]:
                    lines.append(c("  ! %s" % e, "yellow"))

    # 백엔드 직접 probe
    if settings["probe_backends"]:
        brows = []
        for b in snap["backends"]:
            status = c("UP", "green") if b["up"] else c("DOWN", "red")
            models = ",".join(b["models"]) if b["models"] else (b.get("error") or "-")
            brows.append([status, b["name"], b["type"], models])
        lines.append(c("  [Backends] (direct /v1/models probe)", "bold"))
        lines.append(indent(_table(
            ["STATUS", "NAME", "TYPE", "MODELS"], brows), 2))

    return "\n".join(lines)


def indent(text, n):
    pad = " " * n
    return "\n".join(pad + ln for ln in text.split("\n"))


# ----------------------------------------------------------------------------
# Demo data
# ----------------------------------------------------------------------------


def demo_snapshot():
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "litellm": {
            "url": "http://litellm:4000 (demo)",
            "reachable": True,
            "groups": [
                {"model_group": "KServe-Qwen3.6-35B-A3B-FP8",
                 "providers": ["openai"], "mode": "chat",
                 "rpm": 600, "tpm": 400000},
                {"model_group": "SGlang-Qwen3.6-27B-FP8",
                 "providers": ["openai"], "mode": "chat", "rpm": 120},
                {"model_group": "Qwen3-Embedding-8B",
                 "providers": ["openai"], "mode": "embedding"},
            ],
            # /model/info: model_name -> api_base (여기서 실제 주소가 나온다)
            #              + LB 뒤 backend Pod 개수 (k8s EndpointSlice 등)
            "deployments": [
                {"model_name": "KServe-Qwen3.6-35B-A3B-FP8",
                 "underlying": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                 "api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1",
                 "id": "a1b2c3", "type": "kserve",
                 "backends_ready": 3, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "kserve",
                 "service": "qwen36-35b-predictor"},
                {"model_name": "SGlang-Qwen3.6-27B-FP8",
                 "underlying": "hosted_vllm/Qwen3.6-27B-FP8",
                 "api_base": "http://qwen36-27b-sglang.serving.svc:30000/v1",
                 "id": "d4e5f6", "type": "sglang",
                 "backends_ready": 1, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "qwen36-27b-sglang"},
                {"model_name": "vLLM-Stack-Qwen3-32B-AWQ",
                 "underlying": "hosted_vllm/Qwen3-32B-AWQ",
                 "api_base": "http://qwen3-32b-vllm.serving.svc:8000/v1",
                 "id": "g7h8i9", "type": "vllm",
                 "backends_ready": 0, "backends_desired": 2,
                 "backend_source": "deployment", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "qwen3-32b-vllm"},
                {"model_name": "Qwen3-Embedding-8B",
                 "underlying": "openai/Qwen3-Embedding-8B",
                 "api_base": "http://qwen3-embd-predictor.kserve.svc:8080/v1",
                 "id": "j1k2l3", "type": "kserve",
                 "backends_ready": 0, "backends_desired": 0,
                 "backend_source": "knative-pa", "mode": "Serverless",
                 "scale_to_zero": True, "namespace": "kserve",
                 "service": "qwen3-embd-predictor"},
            ],
            "health": {
                "healthy_count": 3,
                "unhealthy_count": 1,
                "healthy_endpoints": [
                    {"model": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                     "api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"},
                    {"model": "hosted_vllm/Qwen3.6-27B-FP8",
                     "api_base": "http://qwen36-27b-sglang.serving.svc:30000/v1"},
                    {"model": "openai/Qwen3-Embedding-8B",
                     "api_base": "http://qwen3-embd-predictor.kserve.svc:8080/v1"},
                ],
                "unhealthy_endpoints": [
                    {"model": "hosted_vllm/Qwen3-32B-AWQ",
                     "api_base": "http://qwen3-32b-vllm.serving.svc:8000/v1"},
                ],
            },
            "models": ["KServe-Qwen3.6-35B-A3B-FP8", "SGlang-Qwen3.6-27B-FP8",
                       "vLLM-Stack-Qwen3-32B-AWQ", "Qwen3-Embedding-8B"],
            "errors": [],
        },
        "backends": [],
        "backend_count_enabled": True,
        "summary": {},
    }
    # 데모에서도 자동 발견 + probe 결과처럼 보이게 backends 채움
    snap["backends"] = [
        {"name": "KServe-Qwen3.6-35B-A3B-FP8",
         "url": "http://qwen36-35b-predictor.kserve.svc:8080",
         "type": "kserve", "up": True,
         "models": ["Qwen3.6-35B-A3B-FP8"], "error": None},
        {"name": "SGlang-Qwen3.6-27B-FP8",
         "url": "http://qwen36-27b-sglang.serving.svc:30000",
         "type": "sglang", "up": True,
         "models": ["Qwen3.6-27B-FP8"], "error": None},
        {"name": "vLLM-Stack-Qwen3-32B-AWQ",
         "url": "http://qwen3-32b-vllm.serving.svc:8000",
         "type": "vllm", "up": False, "models": [], "error": "connection error"},
    ]
    # 사용량(요청 수/토큰) — 실제로는 LiteLLM /global/activity/model 등에서 온다.
    minutes = 24 * 60.0
    demo_usage = {
        "KServe-Qwen3.6-35B-A3B-FP8": (12840, 4820000, 0.0),
        "SGlang-Qwen3.6-27B-FP8": (4321, 1205400, 0.0),
        "Qwen3-Embedding-8B": (30219, 812000, 0.0),
        "vLLM-Stack-Qwen3-32B-AWQ": (152, 41300, 0.0),
    }
    models = {}
    for name, (req, tok, spend) in demo_usage.items():
        models[name] = {
            "requests": req, "tokens": tok, "spend": spend,
            "requests_per_min": round(req / minutes, 3),
            "tokens_per_min": round(tok / minutes, 1),
        }
    snap["usage"] = {
        "source": "/global/activity/model (demo)",
        "granularity": "day",
        "window_hours": 24.0,
        "window_minutes": minutes,
        "start": "(demo)", "end": snap["ts"],
        "models": models,
        "totals": {
            "requests": sum(v[0] for v in demo_usage.values()),
            "tokens": sum(v[1] for v in demo_usage.values()),
            "spend": 0.0,
            "requests_per_min": round(
                sum(v[0] for v in demo_usage.values()) / minutes, 3),
            "models_used": len(demo_usage),
        },
        "errors": [],
    }
    # 현재 부하(live) — 실제로는 백엔드 /metrics(vLLM/SGLang 게이지)에서 온다.
    demo_live = {
        "http://qwen36-35b-predictor.kserve.svc:8080": {
            "engine": "vllm", "running": 7, "waiting": 2, "kv_cache_pct": 63.4},
        "http://qwen36-27b-sglang.serving.svc:30000": {
            "engine": "sglang", "running": 3, "waiting": 0, "kv_cache_pct": 28.9},
        "http://qwen3-32b-vllm.serving.svc:8000": {
            "engine": None, "running": None, "waiting": None, "kv_cache_pct": None,
            "error": "connection error: [Errno 111] Connection refused"},
    }
    for base, live in demo_live.items():
        live["url"] = base + "/metrics"

    snap["litellm"]["groups"].sort(
        key=lambda g: str(g.get("model_group") or "").lower())
    snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])
    snap["litellm"]["deployments"] = attach_usage_to_deployments(
        snap["litellm"], snap["usage"])
    snap["litellm"]["deployments"] = attach_live_to_deployments(
        snap["litellm"]["deployments"], demo_live)
    snap["live_metrics_enabled"] = True
    snap["summary"] = summarize(snap)
    return snap


# ----------------------------------------------------------------------------
# Web 대시보드 (--serve): stdlib http.server, 인라인 self-contained 페이지
# ----------------------------------------------------------------------------

import http.server  # noqa: E402

_DASHBOARD_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Monitor — LiteLLM / KServe / vLLM·SGLang</title>
<style>
  :root{
    --bg:#0d1117; --surface:#161b22; --surface2:#1b222c; --border:#232c38;
    --text:#e6edf3; --muted:#8b97a7; --faint:#5b6675;
    --accent:#6e8bff; --up:#3fb950; --down:#f85149; --warn:#d29922;
    --mono:ui-monospace,"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
    font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1240px;margin:0 auto;padding:20px 22px 60px}
  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
    padding-bottom:16px;border-bottom:1px solid var(--border);margin-bottom:20px}
  h1{font-size:17px;font-weight:650;margin:0;letter-spacing:-.01em}
  h1 .dim{color:var(--faint);font-weight:400}
  h1 .ver{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--accent);
    background:rgba(110,139,255,.12);border:1px solid rgba(110,139,255,.3);
    border-radius:5px;padding:1px 6px;vertical-align:middle}
  .chain{font-family:var(--mono);font-size:11.5px;color:var(--muted);
    background:var(--surface);border:1px solid var(--border);border-radius:6px;
    padding:3px 9px}
  .spacer{flex:1}
  .meta{font-family:var(--mono);font-size:12px;color:var(--muted);
    display:flex;align-items:center;gap:14px}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
  .dot.live{background:var(--up);box-shadow:0 0 0 0 rgba(63,185,80,.5);
    animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.45)}
    70%{box-shadow:0 0 0 6px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
  @media (prefers-reduced-motion:reduce){.dot.live{animation:none}}
  .toggle{font-family:var(--sans);font-size:12px;color:var(--muted);cursor:pointer;
    user-select:none;display:flex;align-items:center;gap:6px}
  .toggle input{accent-color:var(--accent)}
  a.exp{font-family:var(--sans);font-size:12px;color:var(--muted);text-decoration:none;
    border:1px solid var(--border);border-radius:6px;padding:3px 9px;cursor:pointer}
  a.exp:hover{color:var(--text);border-color:var(--accent)}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px;margin-bottom:22px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:9px;
    padding:14px 16px}
  .card .label{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
    color:var(--muted);margin-bottom:7px}
  .card .val{font-family:var(--mono);font-size:26px;font-weight:600;
    font-variant-numeric:tabular-nums;line-height:1}
  .card .sub{font-family:var(--mono);font-size:11.5px;color:var(--faint);margin-top:6px}
  .val.good{color:var(--up)} .val.bad{color:var(--down)} .val.warn{color:var(--warn)}
  .val.accent{color:var(--accent)}

  section{margin-bottom:26px}
  .sec-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;
    color:var(--muted);margin:0 0 10px;display:flex;align-items:center;gap:8px}
  .sec-title .src{font-family:var(--mono);text-transform:none;letter-spacing:0;
    color:var(--faint);font-size:11px}
  .filters{display:flex;align-items:center;gap:7px;margin-left:auto;
    text-transform:none;letter-spacing:0}
  .filters label{font-size:10px;color:var(--faint)}
  .filters select{font-family:var(--sans);font-size:12px;color:var(--text);
    background:var(--surface2);border:1px solid var(--border);border-radius:6px;
    padding:3px 7px;cursor:pointer}
  .filters .fcount{font-family:var(--mono);font-size:11px;color:var(--faint);
    min-width:54px;text-align:right}

  .tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:9px;
    background:var(--surface)}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
    text-align:left;font-weight:500;padding:10px 14px;border-bottom:1px solid var(--border);
    white-space:nowrap}
  td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:var(--surface2)}
  td.mono,th.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
  td.api{font-family:var(--mono);font-size:12px;color:var(--muted);max-width:340px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  td.name{font-weight:550;white-space:nowrap}

  .pill{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);
    font-size:11px;font-weight:600;padding:2px 9px;border-radius:20px;
    border:1px solid transparent}
  .pill.up{color:var(--up);background:rgba(63,185,80,.10);border-color:rgba(63,185,80,.3)}
  .pill.down{color:var(--down);background:rgba(248,81,73,.10);border-color:rgba(248,81,73,.3)}
  .pill.unk{color:var(--muted);background:rgba(139,151,167,.1);border-color:var(--border)}
  .chip{font-family:var(--mono);font-size:11px;color:var(--muted);
    background:var(--surface2);border:1px solid var(--border);border-radius:5px;
    padding:1px 7px}

  /* BACKENDS 셀: ready/desired 미니바 + 수치 */
  .bk{display:flex;align-items:center;gap:9px}
  .bk .num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;
    min-width:46px}
  .bk .bar{flex:1;max-width:90px;height:6px;border-radius:3px;background:var(--surface2);
    overflow:hidden;border:1px solid var(--border)}
  .bk .bar i{display:block;height:100%;border-radius:3px}
  .bk.good .num{color:var(--up)} .bk.good .bar i{background:var(--up)}
  .bk.warn .num{color:var(--warn)} .bk.warn .bar i{background:var(--warn)}
  .bk.bad .num{color:var(--down)} .bk.bad .bar i{background:var(--down)}
  .bk.zero .num{color:var(--warn)}
  .bk .note{font-size:10.5px;color:var(--faint)}
  .srccol{font-family:var(--mono);font-size:11px;color:var(--faint)}

  /* 사용량/부하 셀 */
  .use{display:flex;align-items:center;gap:8px;white-space:nowrap}
  .use .num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px}
  .use .pct{font-family:var(--mono);font-size:11px;color:var(--faint)}
  .use.good .pct{color:var(--up)} .use.warn .pct{color:var(--warn)}
  .use.bad .pct{color:var(--down)}
  .use .bar{width:52px;height:5px;border-radius:3px;background:var(--surface2);
    overflow:hidden;border:1px solid var(--border)}
  .use .bar i{display:block;height:100%}
  .use.good .bar i{background:var(--up)} .use.warn .bar i{background:var(--warn)}
  .use.bad .bar i{background:var(--down)}
  .wait{font-family:var(--mono);font-size:11px;color:var(--warn)}
  .idle{color:var(--faint)}

  .empty{color:var(--muted);padding:18px;text-align:center;font-style:italic}
  .err{color:var(--down);font-family:var(--mono);font-size:12px;
    background:rgba(248,81,73,.07);border:1px solid rgba(248,81,73,.25);
    border-radius:7px;padding:9px 12px;margin-top:8px}
  #banner:not(:empty){margin-bottom:16px}
  .note-banner{color:var(--warn);font-family:var(--mono);font-size:12px;
    background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.3);
    border-radius:7px;padding:9px 12px}
  footer{margin-top:30px;color:var(--faint);font-size:11.5px;font-family:var(--mono);
    border-top:1px solid var(--border);padding-top:14px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Model Monitor <span class="ver" id="ver"></span>
      <span class="dim">· 떠 있는 모델 &amp; LB 뒤 backend</span></h1>
    <span class="chain">LiteLLM → KServe → vLLM / SGLang</span>
    <span class="spacer"></span>
    <label class="toggle"><input type="checkbox" id="auto" checked> auto-refresh</label>
    <a class="exp" href="/snapshot.json" title="현재 상태를 raw JSON 파일로 다운로드 (공유용)">💾 JSON</a>
    <a class="exp" href="/snapshot.html" target="_blank" title="현재 상태를 정지된 self-contained 페이지로 열기 (저장해서 공유)">정지 페이지</a>
    <div class="meta">
      <span><span class="dot live" id="livedot"></span><span id="updated">…</span></span>
    </div>
  </header>

  <div id="banner"></div>

  <div class="cards" id="cards"></div>

  <section id="deployments-sec">
    <div class="sec-title">Deployments
      <span class="src" id="dep-src">/model/info api_base · /health status · k8s backend pods</span>
      <span class="filters">
        <label for="f-status">status</label>
        <select id="f-status">
          <option value="">all</option>
          <option value="UP">UP</option>
          <option value="DOWN">DOWN</option>
          <option value="?">?</option>
        </select>
        <label for="f-type">type</label>
        <select id="f-type">
          <option value="">all</option>
          <option value="vllm">vllm</option>
          <option value="sglang">sglang</option>
          <option value="kserve">kserve</option>
          <option value="-">-</option>
        </select>
        <span class="fcount" id="f-count"></span>
      </span>
    </div>
    <div class="tablewrap"><table id="deployments">
      <thead></thead><tbody></tbody>
    </table></div>
    <div id="dep-err"></div>
  </section>

  <section id="groups-sec">
    <div class="sec-title">Model Groups <span class="src">/model_group/info</span></div>
    <div class="tablewrap"><table id="groups"><thead></thead><tbody></tbody></table></div>
  </section>

  <footer id="foot">model_monitor · 표준 라이브러리만 사용 · 데이터 출처는 LiteLLM + Kubernetes API</footer>
</div>

<script>
const REFRESH_MS = __INTERVAL_MS__;
const $ = (s)=>document.querySelector(s);
let lastSnap = null;   // 필터 변경 시 재수집 없이 다시 렌더하려고 보관

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

function backendCell(d){
  const r=d.backends_ready, des=d.backends_desired, src=d.backend_source||"none";
  const errTip = d.k8s_error ? ' title="'+esc(d.k8s_error)+'"' : '';
  if(src==="external") return '<span class="srccol">external</span>';
  if(d.scale_to_zero) return '<div class="bk zero"><span class="num">0</span>'
    +'<span class="note">scaled-to-zero</span></div>';
  if(r==null){
    // 수집 실패 -> ? 에 마우스 올리면 원인(k8s_error) 표시
    return '<span class="srccol"'+errTip+'>?'+(d.k8s_error?' ⚠':'')+'</span>';
  }
  let cls="good";
  if(r===0) cls="bad"; else if(des!=null && r<des) cls="warn";
  const num = des!=null ? (r+"/"+des) : (""+r);
  let pct = des && des>0 ? Math.min(100, Math.round(r/des*100)) : (r>0?100:0);
  return '<div class="bk '+cls+'"><span class="num">'+num+'</span>'
    +'<span class="bar"><i style="width:'+pct+'%"></i></span></div>';
}

function fmtNum(n){
  if(n==null) return "?";
  n=Number(n);
  const u=[["G",1e9],["M",1e6],["k",1e3]];
  for(const [s,d] of u){ if(Math.abs(n)>=d) return (n/d).toFixed(1)+s; }
  return String(Math.round(n));
}
function utilCls(r){ return r==null?"":(r>=0.9?"bad":(r>=0.7?"warn":"good")); }

// window 누적 요청 수
function reqCell(d){
  const u=d.usage;
  if(!u || u.requests==null) return '<span class="srccol">?</span>';
  const tip = u.tokens!=null ? ' title="tokens '+fmtNum(u.tokens)+'"' : '';
  return '<span class="use'+(u.requests?'':' idle')+'"'+tip+'>'
    +'<span class="num">'+fmtNum(u.requests)+'</span></span>';
}

// 분당 요청 + (한도가 있으면) 사용률 바
function rateCell(d){
  const u=d.usage;
  if(!u || u.requests_per_min==null) return '<span class="srccol">?</span>';
  const rpm=Number(u.requests_per_min);
  let html='<span class="num">'+rpm.toFixed(2)+'</span>';
  if(u.rpm_util!=null){
    const cls=utilCls(u.rpm_util);
    const pct=Math.min(100, Math.round(u.rpm_util*100));
    html='<span class="num">'+rpm.toFixed(2)+'</span>'
      +'<span class="bar"><i style="width:'+pct+'%"></i></span>'
      +'<span class="pct">'+pct+'%</span>';
    return '<div class="use '+cls+'" title="rpm 한도 '+esc(u.rpm_limit)
      +' 대비 사용률">'+html+'</div>';
  }
  return '<div class="use'+(rpm?'':' idle')+'" title="분당 요청(집계 구간 평균)">'
    +html+'</div>';
}

// 지금 처리 중/대기 중인 요청 (백엔드 /metrics 게이지)
function liveCell(d){
  const l=d.live;
  if(!l) return '<span class="srccol">-</span>';
  if(l.error) return '<span class="srccol" title="'+esc(l.error)+'">? ⚠</span>';
  if(l.running==null && l.waiting==null) return '<span class="srccol">?</span>';
  let html='<span class="num">'+(l.running==null?"?":l.running)+'</span>';
  if(l.waiting) html+='<span class="wait">+'+l.waiting+' wait</span>';
  return '<div class="use '+(l.waiting?"warn":(l.running?"good":""))
    +'" title="engine '+esc(l.engine||"?")+' · LB 뒤 Pod 1개 샘플">'+html+'</div>';
}

// KV 캐시 사용률 = GPU 메모리가 실제로 얼마나 물려 있는지
function kvCell(d){
  const l=d.live;
  if(!l) return '<span class="srccol">-</span>';
  if(l.kv_cache_pct==null) return '<span class="srccol">?</span>';
  const pct=Math.min(100, Math.round(l.kv_cache_pct));
  return '<div class="use '+utilCls(pct/100)+'" title="KV cache 사용률">'
    +'<span class="num">'+pct+'%</span>'
    +'<span class="bar"><i style="width:'+pct+'%"></i></span></div>';
}

function statusPill(s){
  const cls = s==="UP"?"up":(s==="DOWN"?"down":"unk");
  return '<span class="pill '+cls+'">'+esc(s)+'</span>';
}

function card(label,val,cls,sub){
  return '<div class="card"><div class="label">'+esc(label)+'</div>'
    +'<div class="val '+(cls||"")+'">'+val+'</div>'
    +(sub?'<div class="sub">'+sub+'</div>':'')+'</div>';
}

function render(snap){
  const s = snap.summary||{};
  const ll = snap.litellm;
  // 수집 상태 배너: 초기 로딩 / 백그라운드 수집 실패를 화면에 노출한다.
  const banner = $("#banner");
  if(snap.loading){
    banner.innerHTML = '<div class="note-banner">⏳ 첫 스냅샷 수집 중…'
      + (snap.error ? ' ('+esc(snap.error)+')' : '') + '</div>';
    $("#updated").textContent = "loading…";
    $("#livedot").style.background = "var(--warn)";
    return;
  }
  banner.innerHTML = snap.collect_error
    ? '<div class="err">⚠ 백그라운드 수집 실패(직전 스냅샷 표시 중): '
      + esc(snap.collect_error) + '</div>'
    : "";
  lastSnap = snap;
  // summary cards
  let cards = "";
  cards += card("Model Groups", s.model_groups||0, "accent");
  cards += card("Registered", s.deployments_registered||0, "");
  cards += card("Running (healthy)", s.deployments_healthy||0, "good",
    "unhealthy "+(s.deployments_unhealthy||0));
  if(s.backend_pods_known)
    cards += card("Backend Pods", (s.backend_pods_ready||0)
      +'<span style="color:var(--faint);font-size:16px"> / '
      +(s.backend_pods_desired||"?")+'</span>', "",
      "LB 뒤 ready / desired");
  if(s.usage_known)
    cards += card("Requests ("+(s.usage_window_hours||24)+"h)",
      fmtNum(s.usage_requests), "accent",
      Number(s.usage_rpm||0).toFixed(1)+" req/min · "
      +(s.usage_models_used||0)+" model 사용");
  if(s.usage_known && s.usage_tokens)
    cards += card("Tokens ("+(s.usage_window_hours||24)+"h)",
      fmtNum(s.usage_tokens), "", "누적 total tokens");
  if(s.live_known)
    cards += card("In-flight", s.live_running||0,
      (s.live_waiting?"warn":"good"),
      (s.live_waiting||0)+" 대기 · 백엔드 /metrics");
  $("#cards").innerHTML = cards;

  // deployments
  const showBk = !!snap.backend_count_enabled;
  const usage = snap.usage||{};
  const showUse = !!(usage.models && Object.keys(usage.models).length);
  const showLive = !!snap.live_metrics_enabled;
  const win = usage.window_hours||24;
  const srcEl=$("#dep-src");
  if(srcEl) srcEl.textContent = "/model/info api_base · /health status"
    + (showBk ? " · k8s backend pods" : "")
    + (showUse ? " · usage "+(usage.source||"") : "")
    + (showLive ? " · live /metrics" : "");
  const dt = $("#deployments");
  if(ll && ll.deployments && ll.deployments.length){
    const all = ll.deployments;
    const fS = $("#f-status").value, fT = $("#f-type").value;
    const merged = all.filter(d=>
      (!fS || (d.status||"?")===fS) && (!fT || (d.type||"-")===fT));
    $("#f-count").textContent = (fS||fT)
      ? merged.length+" / "+all.length : all.length+"";
    let head = "<tr><th>STATUS</th><th>MODEL_NAME</th><th>TYPE</th>";
    if(showBk) head += '<th>BACKENDS (ready/desired)</th>';
    if(showUse) head += '<th class="num">REQ ('+win+'h)</th><th class="num">RPM</th>';
    if(showLive) head += '<th class="num">IN-FLIGHT</th><th class="num">KV CACHE</th>';
    if(showBk) head += '<th>MODE</th><th>SRC</th>';
    head += "<th>API_BASE</th></tr>";
    dt.querySelector("thead").innerHTML = head;
    dt.querySelector("tbody").innerHTML = merged.length ? merged.map(d=>{
      let row = "<tr><td>"+statusPill(d.status||"?")+"</td>"
        +'<td class="name">'+esc(d.model_name)+"</td>"
        +'<td><span class="chip">'+esc(d.type||"-")+"</span></td>";
      if(showBk) row += "<td>"+backendCell(d)+"</td>";
      if(showUse) row += "<td>"+reqCell(d)+"</td><td>"+rateCell(d)+"</td>";
      if(showLive) row += "<td>"+liveCell(d)+"</td><td>"+kvCell(d)+"</td>";
      if(showBk) row +=
        '<td class="mono" style="font-size:12px;color:var(--muted)">'+esc(d.mode||"-")+"</td>"
        +'<td class="srccol">'+esc(d.backend_source||"-")+"</td>";
      row += '<td class="api" title="'+esc(d.api_base)+'">'+esc(d.api_base||"-")+"</td></tr>";
      return row;
    }).join("") : '<tr><td class="empty" colspan="11">필터 결과 없음</td></tr>';
  } else {
    $("#f-count").textContent="";
    dt.querySelector("thead").innerHTML="";
    dt.querySelector("tbody").innerHTML='<tr><td class="empty">deployment 없음 (LiteLLM /model/info 응답 비어있음 또는 미연결)</td></tr>';
  }
  let depErr = (ll && ll.errors && ll.errors.length)
    ? ll.errors.map(e=>'<div class="err">! '+esc(e)+'</div>').join("") : "";
  if(!showUse && usage.errors && usage.errors.length)
    depErr += '<div class="note-banner" style="margin-top:8px">사용량(요청 수) 수집 실패 — '
      +'LiteLLM 분석 엔드포인트 응답 없음/권한 부족: '+esc(usage.errors.slice(0,2).join("; "))
      +'</div>';
  $("#dep-err").innerHTML = depErr;

  // groups
  const gt = $("#groups");
  if(ll && ll.groups && ll.groups.length){
    gt.querySelector("thead").innerHTML="<tr><th>MODEL_GROUP</th><th>PROVIDERS</th><th>MODE</th>"
      +(showUse?'<th class="num">REQ ('+win+'h)</th><th class="num">TOKENS</th>':"")
      +'<th class="num">RPM / TPM 한도</th></tr>';
    const um = usage.models||{};
    gt.querySelector("tbody").innerHTML = ll.groups.map(g=>{
      const u = um[g.model_group];
      let r='<tr><td class="name">'+esc(g.model_group)+"</td>"
      +'<td class="mono" style="color:var(--muted)">'+esc((g.providers||[]).join(", ")||"-")+"</td>"
      +"<td>"+esc(g.mode||"-")+"</td>";
      if(showUse) r+='<td class="mono">'+(u?fmtNum(u.requests):'<span class="srccol">?</span>')+"</td>"
        +'<td class="mono">'+(u&&u.tokens!=null?fmtNum(u.tokens):'<span class="srccol">?</span>')+"</td>";
      r+='<td class="mono" style="color:var(--muted)">'
        +((g.rpm||g.tpm)?((g.rpm||"-")+" / "+(g.tpm||"-")):'<span class="srccol">무제한</span>')+"</td></tr>";
      return r;
    }).join("");
  } else {
    gt.querySelector("thead").innerHTML="";
    gt.querySelector("tbody").innerHTML='<tr><td class="empty">model group 없음</td></tr>';
  }

  if(snap.version) $("#ver").textContent = "v"+snap.version;
  $("#foot").textContent = "model_monitor v"+(snap.version||"?")
    +" · 표준 라이브러리만 사용 · 데이터 출처는 LiteLLM + Kubernetes API";
  $("#updated").textContent = (snap.ts||"") + (snap.demo?"  (demo)":"");
}

async function tick(){
  // 정지 스냅샷(/snapshot.html)으로 열렸으면 폴링 없이 박제된 데이터만 렌더한다.
  if(window.__SNAPSHOT__){
    render(window.__SNAPSHOT__);
    $("#livedot").style.background = "var(--warn)";
    document.querySelectorAll(".exp").forEach(e=>e.style.display="none");
    const a=$("#auto"); if(a){ a.checked=false; a.disabled=true; }
    const u=$("#updated"); if(u) u.textContent += "  · saved snapshot (frozen)";
    return;
  }
  try{
    const r = await fetch("/api/snapshot",{cache:"no-store"});
    const snap = await r.json();
    render(snap);
    $("#livedot").style.background = "var(--up)";
  }catch(e){
    $("#livedot").style.background = "var(--down)";
    $("#updated").textContent = "수집 실패: "+e;
  }
}

let timer=null;
function loop(){ if(window.__SNAPSHOT__) return;   // frozen: 폴링 안 함
  if(timer) clearInterval(timer);
  if($("#auto").checked){ timer=setInterval(tick, REFRESH_MS); } }
$("#auto").addEventListener("change", ()=>{ loop(); if($("#auto").checked) tick(); });
// 필터 변경: 재수집 없이 마지막 스냅샷으로 즉시 다시 렌더
$("#f-status").addEventListener("change", ()=>{ if(lastSnap) render(lastSnap); });
$("#f-type").addEventListener("change", ()=>{ if(lastSnap) render(lastSnap); });
tick(); loop();
</script>
</body>
</html>
"""


def serve_dashboard(settings, host, port, interval, demo):
    """웹 대시보드 서버.

    수집(특히 LiteLLM /health 는 느림)을 백그라운드 스레드에서 주기적으로 수행하고,
    HTTP 요청에는 마지막으로 수집한 스냅샷을 즉시 돌려준다 -> 브라우저가 멈추거나
    BrokenPipe 가 나지 않는다.
    """
    html = _DASHBOARD_HTML.replace("__INTERVAL_MS__", str(int(interval * 1000)))

    def frozen_html(snap):
        """현재 스냅샷을 페이지에 박제 -> 폴링 없이 그대로 렌더되는 self-contained HTML.

        '<' 를 이스케이프해 데이터 안의 </script> 등이 HTML 을 깨지 않게 한다.
        라이브 대시보드와 같은 렌더 코드를 쓰므로 stale 될 일이 없다.
        """
        blob = json.dumps(snap, ensure_ascii=False).replace("<", "\\u003c")
        inject = "<script>window.__SNAPSHOT__=%s;</script>\n</head>" % blob
        return html.replace("</head>", inject, 1)

    state = {"snap": None, "err": None}
    lock = threading.Lock()
    # /health 는 수십 초 걸려서 메인 수집을 막지 않도록 별도 스레드가 캐시에 채운다.
    hcache = {"data": None}
    hlock = threading.Lock()

    def collect_once():
        # 메인 수집은 health 없이(빠름). backend 개수·모델 목록은 interval 마다 갱신.
        snap = demo_snapshot() if demo else build_snapshot(settings, with_health=False)
        snap["demo"] = bool(demo)
        if not demo and snap.get("litellm"):
            with hlock:
                h = hcache["data"]
            if h is not None:
                # 비동기로 받아둔 /health 를 주입하고 status/summary 재계산
                snap["litellm"]["health"] = h
                snap["litellm"]["deployments"] = merge_deployments_with_health(
                    snap["litellm"])
                snap["summary"] = summarize(snap)
        with lock:
            state["snap"] = snap
            state["err"] = None

    def health_loop():
        # 느린 /health 를 천천히(>=30s) 따로 수집. 도착하면 다음 collect 에 반영됨.
        url, key = settings.get("litellm_url"), settings.get("api_key")
        ht = settings.get("health_timeout", 90.0)
        while True:
            try:
                h = fetch_health(url, key, ht)
                if h is not None:
                    with hlock:
                        hcache["data"] = h
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(30.0, interval))

    # 첫 페이지 로드에 데이터가 바로 보이도록 1회 동기 수집(health 제외 -> 즉시)
    try:
        collect_once()
    except Exception as e:  # noqa: BLE001
        state["err"] = "%s: %s" % (type(e).__name__, e)

    def refresh_loop():
        while True:
            time.sleep(max(1.0, interval))
            try:
                collect_once()
            except Exception as e:  # noqa: BLE001
                with lock:
                    state["err"] = "%s: %s" % (type(e).__name__, e)

    threading.Thread(target=refresh_loop, daemon=True).start()
    # health 수집은 settings.health 가 켜져 있고 데모가 아닐 때만
    if not demo and settings.get("health", True) and settings.get("litellm_url"):
        threading.Thread(target=health_loop, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype, extra_headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 클라이언트가 응답 전에 끊음(폴링 취소 등) — 무시

        def _snapshot(self):
            """캐시된 스냅샷(없으면 loading, 수집오류면 collect_error 부착)."""
            with lock:
                snap, err = state["snap"], state["err"]
            if snap is None:
                return {"version": __version__, "loading": True,
                        "error": err, "summary": {}, "litellm": None}
            if err:
                return dict(snap, collect_error=err)
            return snap

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, html, "text/html; charset=utf-8")
            elif path == "/api/snapshot":
                self._send(200, json.dumps(self._snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif path == "/snapshot.json":
                # 브라우저에서 클릭 한 번에 파일로 받게 attachment 로 내려준다.
                self._send(200, json.dumps(self._snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="model-monitor-snapshot.json"'})
            elif path in ("/snapshot.html", "/export"):
                # 데이터가 박제된 self-contained 페이지(폴링 없음) — 저장해서 공유용.
                self._send(200, frozen_html(self._snapshot()),
                           "text/html; charset=utf-8")
            elif path in ("/healthz", "/readyz"):
                self._send(200, "ok", "text/plain")
            else:
                self._send(404, "not found", "text/plain")

        def log_message(self, *a):  # 액세스 로그 억제
            pass

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d" % ("localhost" if host == "0.0.0.0" else host, port)
    print("Model Monitor 웹 대시보드: %s  (%.0fs 갱신, Ctrl+C 종료)"
          % (url, interval))
    print("  스냅샷 내보내기: %s/snapshot.json (raw JSON 다운로드)"
          "  ·  %s/snapshot.html (정지 페이지)" % (url, url))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
        httpd.shutdown()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def clear_screen():
    sys.stdout.write("\033[2J\033[H")


def run_once(settings, as_json, demo):
    snap = (demo_snapshot() if demo
            else build_snapshot(settings, with_health=settings.get("health", True)))
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print(render(snap, settings if not demo else dict(settings, probe_backends=True)))
    return snap


def main():
    p = argparse.ArgumentParser(
        description="LiteLLM/KServe/vLLM-SGLang 모델 현황 모니터 (v%s)" % __version__)
    p.add_argument("--version", action="version",
                   version="model_monitor %s" % __version__)
    p.add_argument("--litellm-url", help="LiteLLM 게이트웨이 URL (예: http://litellm:4000)")
    p.add_argument("--api-key", help="LiteLLM API key (admin 권한 권장)")
    p.add_argument("--config", help="설정 파일 (.json, 또는 PyYAML 있으면 .yaml)")
    p.add_argument("--probe-backends", action="store_true",
                   help="설정의 backends 를 직접 /v1/models probe")
    p.add_argument("--watch", action="store_true", help="실시간 갱신 모드(터미널)")
    p.add_argument("--interval", type=float, default=5.0, help="watch/웹 갱신 주기(초)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.add_argument("--timeout", type=float, default=10.0, help="HTTP 타임아웃(초)")
    p.add_argument("--health-timeout", type=float,
                   help="LiteLLM /health 타임아웃(초, 기본 90 — 모델 많으면 늘리기)")
    p.add_argument("--no-health", action="store_true",
                   help="LiteLLM /health 호출 안 함 (status 는 k8s backend readiness 로 판정)")
    p.add_argument("--demo", action="store_true", help="샘플 데이터로 미리보기")
    # 사용량 / 현재 부하
    p.add_argument("--no-usage", action="store_true",
                   help="모델별 사용량(요청 수/토큰) 수집 안 함")
    p.add_argument("--usage-window", type=float,
                   help="사용량 집계 구간(시간, 기본 24)")
    p.add_argument("--probe-metrics", action="store_true",
                   help="백엔드 /metrics 를 직접 읽어 현재 실행/대기 요청·KV 캐시 사용률 표시")
    # 웹 UI
    p.add_argument("--serve", action="store_true",
                   help="웹 대시보드 모드 (브라우저로 조회)")
    p.add_argument("--host", default="0.0.0.0", help="웹 서버 bind host")
    p.add_argument("--port", type=int, default=8088, help="웹 서버 포트")
    # backend 개수(LB 뒤 Pod 수) 수집
    p.add_argument("--no-backend-count", action="store_true",
                   help="LB 뒤 backend Pod 개수 수집 비활성")
    p.add_argument("--k8s-api-server", help="k8s API server URL 오버라이드")
    p.add_argument("--k8s-token-file", help="ServiceAccount 토큰 파일 경로")
    p.add_argument("--k8s-ca-file", help="ServiceAccount CA 파일 경로")
    p.add_argument("--k8s-insecure", action="store_true",
                   help="k8s API TLS 검증 비활성")
    p.add_argument("--k8s-timeout", type=float, help="k8s API 호출 타임아웃(초)")
    args = p.parse_args()

    settings = resolve_settings(args)

    if (not args.demo and not settings["litellm_url"]
            and not settings["backends"]):
        p.error("--litellm-url 또는 --config 가 필요합니다 (또는 --demo).")

    if args.serve:
        serve_dashboard(settings, args.host, args.port, args.interval, args.demo)
        return

    if args.watch:
        try:
            while True:
                clear_screen()
                run_once(settings, args.json, args.demo)
                sys.stdout.write(
                    c("\n(%.0fs 마다 갱신 — Ctrl+C 종료)\n" % args.interval, "dim"))
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
            return
    else:
        run_once(settings, args.json, args.demo)


if __name__ == "__main__":
    main()
