"""LiteLLM 게이트웨이 / 백엔드 직접 probe 수집기.

핵심: api_base 는 /v1/models 가 아니라 /model/info 에서 나온다(admin 키 필요).
/health 는 모든 백엔드를 실제 ping 하므로 느리다(수십 초).
"""

from app.core.http import http_get_json


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


def _strip_openai_suffix(api_base):
    """api_base 에 붙은 OpenAI 경로(/v1, /openai/v1)를 떼어 베이스만 남긴다."""
    base = api_base.rstrip("/")
    for suffix in ("/openai/v1", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


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
        # 이름순 정렬: LiteLLM 응답 순서가 replica 구성에 따라 바뀌어도 표시 고정.
        # 대소문자만 다른 그룹('vllm-X'↔'vLLM-X')은 lower 가 같아 동률이므로,
        # 원문 이름을 2차 키로 둬 순서가 폴링마다 뒤바뀌지 않게 한다.
        result["groups"] = sorted(
            data.get("data", []) or [],
            key=lambda g: (str(g.get("model_group") or "").lower(),
                           str(g.get("model_group") or "")))
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
                "health: %s (모델 많으면 health_timeout 늘리기)" % err)

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
