"""LiteLLM 게이트웨이 / 백엔드 직접 probe 수집기.

핵심: api_base 는 /v1/models 가 아니라 /model/info 에서 나온다(admin 키 필요).
/health 는 모든 백엔드를 실제 ping 하므로 느리다(수십 초).
"""

import urllib.parse

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


def _is_health_payload(data):
    """dict 가 LiteLLM /health 응답 모양인가 (healthy/unhealthy_endpoints 보유)."""
    return isinstance(data, dict) and (
        "healthy_endpoints" in data or "unhealthy_endpoints" in data)


def fetch_health(url, api_key, timeout):
    """LiteLLM /health 만 단독 조회 (느려서 비동기로 돌릴 때 사용). dict|None.

    LiteLLM 은 unhealthy 백엔드가 있으면 HTTP 200 이 아니라 **503 에 동일한
    health payload** 를 실어 보낸다 — 상태코드만 보고 버리면 '전부 DOWN' 같은
    가장 중요한 순간의 상태 정보를 잃으므로, 본문이 health 모양이면 수용한다.
    """
    ok, data, err = http_get_json(url.rstrip("/") + "/health", api_key, timeout)
    if ok and isinstance(data, dict):
        return data
    return data if _is_health_payload(data) else None


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
    Knative 흔적은 전부 False — 최악의 실패가 "체크 안 해서 ?"이지
    "idle 백엔드를 깨움"이 되지 않게 한다.

    Knative 판정은 backend_count 가 내보낸 명시 필드(serverless: _is_serverless
    의 mode+revision 판정, activator_only: EndpointSlice 증거)를 1차로 쓴다 —
    여기서 문자열을 재추론하면 두 정의가 드리프트한다(mode 검사는 구 스냅샷
    호환 겸 이중 안전망).

    model_info.active_health_check 수동 override:
      false → 항상 제외.  true → 판정불가/external 도 체크 허용. 단, k8s 가
      양성으로 위험(Knative/scale-to-zero/activator)을 확인한 경우는 override
      보다 우선한다(잘못 단 마커가 idle 백엔드를 깨우지 않게).
    """
    ahc = d.get("active_health_check")
    if ahc is False:
        return False
    # ── 양성 위험 신호 — override(true)로도 못 뒤집는다 ──────────────────
    if d.get("scale_to_zero") or d.get("serverless") or d.get("activator_only"):
        return False
    mode = str(d.get("mode") or "").lower()
    if ("serverless" in mode) or ("knative" in mode):
        return False
    # knative-pa / knative-revision 둘 다 Knative 경유 카운트 = 위험
    if str(d.get("backend_source") or "").startswith("knative"):
        return False
    if ahc is True:
        return True
    # ── 양성 안전 신호 ──────────────────────────────────────────────────
    nt = d.get("network_type")
    if nt == "kserve":
        # KServe 는 RawDeployment 로 확인된 경우만(activator 없음)
        return mode == "rawdeployment"
    if nt == "service":
        # 'service' 분류는 ISVC 조회 404 에서 나오므로 그 자체는 양성 신호가
        # 아니다(네이밍이 빗나간 KServe Serverless·순수 Knative Service 도 404
        # → service). 실제 Pod 가 비-Knative 경로로 카운트된 경우만 안전 확정 —
        # scale-to-zero 상태면 activator-only 라 카운트가 안 잡혀 여기서 걸러진다.
        return (d.get("backends_ready") is not None
                and d.get("backend_source") in ("endpointslice", "endpoints",
                                                "deployment"))
    return False   # external / '-' / 미상


def select_health_check_models(deployments):
    """능동 health check 대상 model_name 목록 (정렬·중복 제거).

    /health?model=<name> 은 그 이름의 **모든** deployment 를 ping 하므로,
    같은 model_name 에 안전/위험 백엔드가 섞여 있으면(예: RawDeployment +
    Serverless 이중화) 이름 전체를 제외한다 — 하나라도 위험하면 체크 안 함.
    """
    by_name = {}
    for d in deployments or []:
        name = d.get("model_name")
        # "?" 는 /model/info 에 model_name 이 없을 때의 표시용 플레이스홀더 —
        # 실제 모델이 아니므로 /health?model=%3F 같은 무의미 조회를 만들지 않는다.
        if not name or name == "?":
            continue
        by_name.setdefault(name, []).append(d)
    return sorted(name for name, ds in by_name.items()
                  if all(_deployment_health_safe(d) for d in ds))


def fetch_health_for_model(url, api_key, name, timeout):
    """/health?model=<name> 1회 조회 -> (ok, data, err).

    병렬화는 호출측(Refresher)이 asyncio 로 한다 — 여기서 자체 스레드풀을 만들면
    main.py 가 의도적으로 캡한 수집 스레드 예산(_COLLECT_THREADS) 밖의 스레드가
    생긴다. 이 함수는 블로킹 1콜만 담당한다.

    LiteLLM 은 대상 모델이 unhealthy 면 HTTP **503 에 동일한 health payload**
    (healthy/unhealthy_endpoints)를 실어 보낸다 — 이걸 실패로 처리하면 정작
    DOWN 백엔드의 상태가 매번 '조회 실패'로 버려진다. 본문이 health 모양이면
    성공으로 정규화한다.
    """
    q = urllib.parse.quote(name, safe="")
    ok, data, err = http_get_json(
        url.rstrip("/") + "/health?model=" + q, api_key, timeout)
    if not ok and _is_health_payload(data):
        return True, data, None
    return ok, data, err


def aggregate_selective_health(results, allowed_bases=None):
    """모델별 /health?model= 응답들을 전체 /health 모양으로 합친다 (순수 함수).

    results: [(name, ok, data, err), ...] — fetch_health_for_model 결과 나열.
    allowed_bases: {model_name: {base api_base(/v1 등 접미어 제거), ...}} — 주면
      각 응답을 그 모델의 api_base 로 필터한다. LiteLLM 이 ?model= 을 지원하지
      않으면(구버전/쿼리를 떼는 프록시) 응답에 전체 백엔드가 섞여 오는데, 그대로
      집계하면 체크에서 제외한 모델(Serverless 등)의 상태까지 오염된다 — 필터로
      차단하고 감지 사실을 errors 로 남긴다(ping 자체는 서버측이라 못 막지만
      잘못된 상태 주입은 막는다).

    반환: 기존 /health 호환 dict(healthy/unhealthy_endpoints ...). 단, 조회
      대상이 있었는데 **전부 실패**하면 None — 마지막 정상 결과를 빈 결과로
      덮어쓰지 않기 위해(전량 경로 fetch_health 의 실패 시 None 과 같은 계약).
      체크에서 빠진 모델은 어느 목록에도 없으므로 k8s 폴백(→ '?')으로 흐른다.
    """
    names = sorted({r[0] for r in results})
    out = {"healthy_endpoints": [], "unhealthy_endpoints": [],
           "healthy_count": 0, "unhealthy_count": 0,
           "selective": True, "checked_models": names, "errors": []}
    healthy_raw, unhealthy_raw = [], []
    any_ok = False
    foreign = 0
    for name, ok, data, err in results:
        if ok and isinstance(data, dict):
            any_ok = True
            for key, bucket in (("healthy_endpoints", healthy_raw),
                                ("unhealthy_endpoints", unhealthy_raw)):
                for ep in data.get(key) or []:
                    if allowed_bases is not None:
                        base = _strip_openai_suffix(str(ep.get("api_base") or ""))
                        if base not in (allowed_bases.get(name) or ()):
                            foreign += 1
                            continue
                    bucket.append(ep)
        elif err:
            out["errors"].append("health?model=%s: %s" % (name, err))
    if names and not any_ok:
        return None   # 전 모델 조회 실패 — 주입 생략(last-good 유지)
    if foreign:
        out["errors"].append(
            "health?model= 응답에 요청 모델 밖 endpoint %d건 — LiteLLM 이 model "
            "파라미터를 지원하는지 확인 필요(해당 endpoint 는 무시함)" % foreign)
    # (model, api_base) 로 dedup — 공유 backend 는 여러 모델 응답에 중복돼 온다.
    # 같은 endpoint 가 healthy/unhealthy 양쪽에 오면(두 병렬 호출 사이 flap)
    # DOWN 우선: merge 는 healthy 를 먼저 보므로 모순 항목은 unhealthy 에만 남긴다.
    def _sig(ep):
        return (str(ep.get("model")), str(ep.get("api_base")))
    seen_u = set()
    for ep in unhealthy_raw:
        s = _sig(ep)
        if s not in seen_u:
            seen_u.add(s)
            out["unhealthy_endpoints"].append(ep)
    seen_h = set()
    for ep in healthy_raw:
        s = _sig(ep)
        if s in seen_u or s in seen_h:
            continue
        seen_h.add(s)
        out["healthy_endpoints"].append(ep)
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
            # bool() 강제 변환 금지: YAML 에 "false"(따옴표 문자열)로 쓰는 흔한
            # 실수가 bool("false")==True 로 뒤집혀 opt-out 이 opt-in 이 된다.
            # bool 과 명시적 true/false 문자열만 인정, 그 외 값은 무시(fail-safe).
            ahc = mi.get("active_health_check")
            if isinstance(ahc, bool):
                dep["active_health_check"] = ahc
            elif isinstance(ahc, str):
                low = ahc.strip().lower()
                if low in ("true", "1", "yes", "on"):
                    dep["active_health_check"] = True
                elif low in ("false", "0", "no", "off"):
                    dep["active_health_check"] = False
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
