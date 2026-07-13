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

# KServe 판별용 Service 이름 접미사 — **운영 규약**: KServe 로 서빙되는 svc 는
# 항상 '-predictor' 이름을 가진다(운영자 보장, 규약이 바뀌면 여기를 업데이트).
# transformer/explainer 는 KServe 부속 컴포넌트의 동일 계열 네이밍.
_KSERVE_NAME_SUFFIXES = ("-predictor", "-transformer", "-explainer")


def _service_name_of(d):
    """deployment 의 서비스 이름 — backend_count 결과(service) 우선, 없으면
    api_base 호스트의 첫 라벨. k8s 조회가 전혀 안 되는 환경(권한 없음/비활성)
    에서도 이름 규약 판별이 가능하게 한다. IP 주소면 의미 없는 라벨('50' 등)이
    나오지만 접미사 매칭에 걸리지 않아 무해하다."""
    svc = d.get("service")
    if svc:
        return str(svc)
    host = str(d.get("api_base") or "")
    host = host.split("//")[-1].split("/", 1)[0].split(":", 1)[0]
    return host.split(".", 1)[0] if host else ""


def _looks_kserve(d):
    """KServe 로 서빙되는 backend 인가 — 이름 규약(-predictor 등) 또는
    ISVC 실조회 성공(network_type=kserve) 중 하나라도 참이면 True."""
    return (_service_name_of(d).endswith(_KSERVE_NAME_SUFFIXES)
            or d.get("network_type") == "kserve")


def _deployment_health_safe(d):
    """이 deployment 를 능동 health check 해도 안전한가.

    운영 스펙: "KServe 를 제외한 나머지를 LiteLLM /health?model= 로 체크".
    KServe 판별은 서비스 네이밍 규약(-predictor)이 1차 — k8s 없이도 판별되고,
    ISVC 이름 추측이 빗나가 404 로 'service' 분류된 KServe 도 이름으로 잡힌다.

    판별 순서:
      1) 마커 false → 제외
      2) Knative 양성 위험(scale_to_zero / serverless(revision 포함 판정) /
         activator_only / mode 문자열 / knative-* 카운트 소스) → 제외
         — 마커 true 로도 못 뒤집는다(잘못 단 마커가 idle 백엔드를 깨우지 않게)
      3) 마커 true → 체크
      4) KServe(이름 규약 또는 ISVC 확인) → k8s 가 **RawDeployment 로 양성
         확인**한 경우만 체크(activator 없어 ping 안전). Serverless 이거나
         mode 확인 불가(RBAC 실패 등)면 제외(fail-safe — 깨울 수 있는 쪽으로
         안 넘어감)
      5) 나머지(비 KServe: 일반 Service·external IP·판정불가) → 체크
         — ping 은 LiteLLM 이 대신하므로 모니터가 백엔드에 직접 닿지 않는다
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
    if _looks_kserve(d):
        # KServe → RawDeployment 양성 확인 시에만 체크 (이름만으론 Raw/
        # Serverless 를 구분할 수 없으므로 k8s ISVC mode 조회가 그 역할)
        return mode == "rawdeployment"
    return True   # 비 KServe → 체크 (일반 Service·external 포함)


def select_health_check_models(deployments):
    """능동 health check 대상 model_name 목록 (정렬·중복 제거).

    /health?model=<name> 은 그 이름의 **모든** deployment 를 ping 하므로,
    같은 model_name 에 안전/위험 백엔드가 섞여 있으면(예: RawDeployment +
    Serverless 이중화) 이름 전체를 제외한다 — 하나라도 위험하면 체크 안 함.

    나아가 실운영 관측상 LiteLLM 의 ?model= 매칭은 model_name 보다 넓을 수
    있다(같은 underlying 모델의 **다른 이름** deployment 의 endpoint 가 응답에
    포함됨). 안전한 이름이라도 위험한 sibling 과 underlying 또는 api_base 를
    공유하면 그 ping 이 sibling(Serverless)까지 깨울 수 있으므로 함께 제외한다.
    """
    # underlying 은 같은 모델이라도 provider 접두사 유무가 섞인다(실데이터:
    # "openai/Qwen3-Next-..." vs "Qwen3-Next-..."). 접두사를 떼고 비교해야
    # 관측된 교차 ping(접두사 다른 sibling 의 predictor 가 응답에 등장)을 막는다.
    def _norm_underlying(u):
        u = str(u or "")
        return u.split("/", 1)[1] if "/" in u else u

    by_name = {}
    unsafe_underlying, unsafe_base = set(), set()
    for d in deployments or []:
        name = d.get("model_name")
        # "?" 는 /model/info 에 model_name 이 없을 때의 표시용 플레이스홀더 —
        # 실제 모델이 아니므로 /health?model=%3F 같은 무의미 조회를 만들지 않는다.
        if not name or name == "?":
            continue
        by_name.setdefault(name, []).append(d)
        if not _deployment_health_safe(d):
            if d.get("underlying"):
                unsafe_underlying.add(_norm_underlying(d["underlying"]))
            if d.get("api_base"):
                unsafe_base.add(_strip_openai_suffix(d["api_base"]))

    def _name_ok(ds):
        for d in ds:
            if not _deployment_health_safe(d):
                return False
            if (d.get("underlying")
                    and _norm_underlying(d["underlying"]) in unsafe_underlying):
                return False   # 위험 sibling 과 같은 underlying — ping 전파 위험
            if (d.get("api_base")
                    and _strip_openai_suffix(d["api_base"]) in unsafe_base):
                return False   # 위험 sibling 과 같은 backend 공유
        return True

    return sorted(n for n, ds in by_name.items() if _name_ok(ds))


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
    # 체크 대상 전체의 base 합집합 — LiteLLM 의 ?model= 매칭이 이름보다 넓어
    # sibling(다른 체크 모델)의 endpoint 가 섞여 올 수 있는데, 그것까지 버리면
    # 정보 손실이다. merge 는 api_base 기준이라 어느 쿼리로 왔든 정확한 행에
    # 붙으므로, 합집합 안이면 수용하고 **합집합 밖**(=체크에서 제외한 backend
    # 가 ping 된 정황)만 차단·경고한다.
    union = set()
    if allowed_bases is not None:
        for bases in allowed_bases.values():
            union |= set(bases)
    healthy_raw, unhealthy_raw = [], []
    any_ok = False
    foreign = []   # (요청 모델, 밖의 base)
    for name, ok, data, err in results:
        if ok and isinstance(data, dict):
            any_ok = True
            for key, bucket in (("healthy_endpoints", healthy_raw),
                                ("unhealthy_endpoints", unhealthy_raw)):
                for ep in data.get(key) or []:
                    if allowed_bases is not None:
                        base = _strip_openai_suffix(str(ep.get("api_base") or ""))
                        if base not in union:
                            foreign.append((name, base))
                            continue
                    bucket.append(ep)
        elif err:
            out["errors"].append("health?model=%s: %s" % (name, err))
    if names and not any_ok:
        return None   # 전 모델 조회 실패 — 주입 생략(last-good 유지)
    for name, base in foreign[:3]:
        out["errors"].append(
            "health?model=%s 응답에 체크 대상 밖 endpoint(%s) — 체크에서 제외한 "
            "backend(Serverless 등)가 ping 된 정황. 상태 반영은 차단함" % (name, base))
    if len(foreign) > 3:
        out["errors"].append("체크 대상 밖 endpoint 외 %d건" % (len(foreign) - 3))
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

    # /v1/models 의 id 는 /model/info 의 model_name(public name)과 같다(같은 게이트웨이
    # 관점). 이미 model/info 를 받아 deployments 를 채웠으므로, 매 스냅샷 주기(기본 5s)
    # 마다 /v1/models 를 또 호출하지 않고 그 목록에서 유도한다 — 어떤 렌더러도 별도로
    # 안 쓰는 데이터를 위해 LiteLLM 왕복을 하루 수만 번 반복하지 않게 한다. "?"
    # (model_name 미상 플레이스홀더)는 실제 모델이 아니므로 제외.
    result["models"] = sorted(
        {d["model_name"] for d in result["deployments"]
         if d.get("model_name") and d["model_name"] != "?"})

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

    직접 probe 는 LiteLLM 을 경유하지 않고 백엔드에 바로 닿으므로, 선택적 health
    check 와 같은 안전 판정(_deployment_health_safe)으로 위험 백엔드(Serverless/
    scale-to-zero/Raw 확인 안 된 KServe)를 제외한다 — 리프레시 주기(기본 5s)마다
    쏘는 probe 가 idle 백엔드를 깨우거나 scale-down 을 막지 않게. 같은 api_base 를
    안전/위험 deployment 가 공유하면 그 base 전체를 제외한다(하나라도 위험하면 제외).
    build_snapshot 은 backend_count 판정 **뒤**에 이 함수를 부르므로 k8s 필드
    (serverless/scale_to_zero/mode)가 실려 있고, k8s 를 못 보는 환경에서도
    이름 규약(-predictor)이 폴백으로 동작한다.
    """
    discovered = {}
    unsafe = set()   # 위험 deployment 가 쓰는 base — probe 대상에서 제외
    for d in litellm_result.get("deployments") or []:
        if d.get("api_base") and not _deployment_health_safe(d):
            unsafe.add(_strip_openai_suffix(d["api_base"]))
    # 1순위: /model/info 의 deployments (api_base 평문 + 종류 분류 포함)
    for d in litellm_result.get("deployments") or []:
        api_base = d.get("api_base")
        if not api_base:
            continue
        base = _strip_openai_suffix(api_base)
        if base in unsafe:
            continue
        discovered.setdefault(base, {
            "name": d.get("model_name") or base,
            "url": base,
            "type": d.get("type", "-"),
        })
    # 2순위 보강: /health 에만 있는 주소 — deployment 가 없어 k8s 판정이 불가하므로
    # 이름 규약으로만 거른다(KServe 로 보이면 Raw/Serverless 구분이 안 돼 보수적 제외).
    health = litellm_result.get("health") or {}
    for ep in (health.get("healthy_endpoints") or []) + (
            health.get("unhealthy_endpoints") or []):
        api_base = ep.get("api_base")
        if not api_base:
            continue
        base = _strip_openai_suffix(api_base)
        if base in discovered or base in unsafe:
            continue
        if _looks_kserve({"api_base": base}):
            continue
        model = ep.get("model", "")
        btype = _classify_backend(model, model, base)
        discovered[base] = {"name": model or base, "url": base, "type": btype}
    return list(discovered.values())
