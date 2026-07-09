"""LiteLLM 게이트웨이 / 백엔드 직접 probe 수집기.

핵심: api_base 는 /v1/models 가 아니라 /model/info 에서 나온다(admin 키 필요).
/health 는 모든 백엔드를 실제 ping 하므로 느리다(수십 초).
"""

import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from app.core.http import http_get_json


def _classify_backend(model_name, underlying, api_base):
    """(레거시) model_name 접두사/underlying model 로 백엔드 종류 추정 (표시용).

    인프라(kserve)와 엔진(vllm/sglang)이 한 값에 섞여 첫 매칭이 나머지를 가리는
    한계가 있다 — 신규 2축 분류(network_type/backend_type)를 쓰고, 이 값은
    기존 API 소비자 호환용으로만 유지한다.
    """
    blob = ("%s %s %s" % (model_name, underlying, api_base)).lower()
    if "sglang" in blob:
        return "sglang"
    if "kserve" in blob:
        return "kserve"
    if "vllm" in blob:
        return "vllm"
    return "-"


def _classify_engine(model_name, underlying, api_base):
    """이름/모델 문자열로 서빙 엔진(vllm/sglang)만 추정 — 폴백용 휴리스틱.

    k8s Pod 컨테이너(이미지/커맨드) 기반 판정(backend_count 경유)이 있으면
    그쪽이 우선하고, 이 값은 Pod 를 못 보는 경우(GPU 수집 꺼짐/외부 백엔드/
    scale-to-zero)의 폴백이다. 인프라 키워드(kserve)는 여기서 보지 않는다.
    """
    blob = ("%s %s %s" % (model_name, underlying, api_base)).lower()
    if "sglang" in blob:
        return "sglang"
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


# ── 선택적 health check (scale-to-zero 를 깨우지 않는 부분 /health) ──────────
# 전량 /health 는 LiteLLM 이 모든 백엔드를 실제 ping 해서, Knative Serverless
# (scale-to-zero) 백엔드를 깨우거나 scale-down 을 계속 막는다. 그래서 운영에선
# MONITOR_HEALTH=false 로 꺼 두는데, 그러면 non-KServe 백엔드의 능동 체크까지
# 같이 사라진다. 여기서는 k8s 판정(backend_count 가 붙인 mode/scale_to_zero/
# network_type)으로 "찔러도 안전한 모델"만 골라 /health?model=<name> 으로
# 개별 조회한다 — ping 은 LiteLLM 이 자기 자격증명으로 대신하므로 모니터가
# 백엔드 키를 알 필요가 없다.

def _deployment_health_safe(d):
    """이 deployment 를 능동 health check 해도 안전한가 (fail-safe).

    원칙: '안전이 양성으로 확인된 것만' True. 판정 불가('-'/None)·external·
    Serverless/knative 흔적은 전부 False — 최악의 실패가 "체크 안 해서 ?"이지
    "idle 백엔드를 깨움"이 되지 않게 한다.

    model_info.active_health_check 수동 override:
      false → 항상 제외.  true → 판정불가/external 도 체크 허용. 단, k8s 가
      양성으로 위험(Serverless/scale-to-zero)을 확인한 경우는 override 보다
      우선한다(잘못 단 마커가 idle 백엔드를 깨우지 않게).
    """
    ahc = d.get("active_health_check")
    if ahc is False:
        return False
    # 양성 위험 신호 — override(true)로도 못 뒤집는다
    if d.get("scale_to_zero"):
        return False
    mode = str(d.get("mode") or "").lower()
    if ("serverless" in mode) or ("knative" in mode):
        return False
    if d.get("backend_source") == "knative-pa":
        return False
    if ahc is True:
        return True
    # 양성 안전 신호 — KServe 는 RawDeployment 로 확인된 경우만(activator 없음)
    nt = d.get("network_type")
    if nt == "kserve":
        return d.get("mode") == "RawDeployment"
    return nt == "service"   # external / '-' / 미상 → False


def select_health_check_models(deployments):
    """능동 health check 대상 model_name 목록 (정렬·중복 제거).

    /health?model=<name> 은 그 이름의 **모든** deployment 를 ping 하므로,
    같은 model_name 에 안전/위험 백엔드가 섞여 있으면(예: RawDeployment +
    Serverless 이중화) 이름 전체를 제외한다 — 하나라도 위험하면 체크 안 함.
    """
    by_name = {}
    for d in deployments or []:
        name = d.get("model_name")
        if not name:
            continue
        by_name.setdefault(name, []).append(d)
    return sorted(name for name, ds in by_name.items()
                  if all(_deployment_health_safe(d) for d in ds))


def fetch_health_for_models(url, api_key, model_names, timeout, parallel=4):
    """/health?model=<name> 를 모델별로 호출해 전체 /health 모양으로 합친다.

    반환 dict 는 healthy_endpoints/unhealthy_endpoints 를 갖는 기존 /health
    구조와 호환 — merge_deployments_with_health 에 그대로 주입 가능. 체크에서
    빠진 모델은 어느 목록에도 없으므로 k8s readiness 폴백(→ '?')으로 흐른다.
    한 모델 조회 실패는 errors 에 기록만 하고 나머지 수집을 막지 않는다.
    """
    base = url.rstrip("/")
    names = sorted({n for n in (model_names or []) if n})
    out = {"healthy_endpoints": [], "unhealthy_endpoints": [],
           "healthy_count": 0, "unhealthy_count": 0,
           "selective": True, "checked_models": names, "errors": []}
    if not names:
        return out

    def one(name):
        q = urllib.parse.quote(name, safe="")
        return name, http_get_json(base + "/health?model=" + q, api_key, timeout)

    if parallel > 1 and len(names) > 1:
        with ThreadPoolExecutor(max_workers=min(parallel, len(names))) as ex:
            results = list(ex.map(one, names))
    else:
        results = [one(n) for n in names]

    # 공유 backend(여러 model_name 이 한 api_base)는 응답마다 중복돼 들어오므로
    # (model, api_base) 로 dedup — merge 는 set 이라 무해하지만 count 왜곡 방지.
    seen = set()
    for name, (ok, data, err) in results:
        if ok and isinstance(data, dict):
            for key, lst in (("healthy_endpoints", out["healthy_endpoints"]),
                             ("unhealthy_endpoints", out["unhealthy_endpoints"])):
                for ep in data.get(key) or []:
                    sig = (key, str(ep.get("model")), str(ep.get("api_base")))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    lst.append(ep)
        elif err:
            out["errors"].append("health?model=%s: %s" % (name, err))
    out["healthy_count"] = len(out["healthy_endpoints"])
    out["unhealthy_count"] = len(out["unhealthy_endpoints"])
    return out


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
            dep = {
                "model_name": name,
                "underlying": underlying,
                "api_base": api_base,
                "id": mi.get("id"),
                "type": _classify_backend(name, underlying, api_base or ""),
                # 엔진(vllm/sglang) 이름 휴리스틱 — backend_count 가 Pod 이미지로
                # 판정하면 그 값으로 덮어쓴다(backend_type_source="pod").
                "backend_type": _classify_engine(name, underlying, api_base or ""),
                "backend_type_source": "name",
            }
            # 선택적 health check 수동 override — LiteLLM config 의
            # model_info.active_health_check (true=판정불가여도 체크 허용,
            # false=항상 제외). 없으면 k8s 판정(select_health_check_models)만 쓴다.
            ahc = mi.get("active_health_check")
            if ahc is not None:
                dep["active_health_check"] = bool(ahc)
            result["deployments"].append(dep)
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
