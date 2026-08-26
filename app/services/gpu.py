"""GPU: backend Pod 의 nvidia.com/gpu 개수 + 장치 모델명(H100/B200 ...).

개수 -> Pod spec resources.limits["nvidia.com/gpu"]
장치 -> Pod 가 뜬 노드의 라벨 nvidia.com/gpu.product (GPU Operator/GFD)
멀티노드 GPU 환경 없음 전제: Pod 1개 = 노드 1개.
"""

import time
import urllib.parse

GPU_RESOURCE = "nvidia.com/gpu"
GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"

# --- 사이클 간 메타 캐시 (거의 변하지 않는 k8s 조회 결과) -------------------
# 실측(배포 35 / 고유 Service 12 / 5초 주기): 사이클당 Service 마다 k8s 5회 =
# 하루 약 103만 회. 그중 2회는 매번 같은 답을 준다 — ISVC 부재(=이 Service 는
# KServe 가 아니다)와 Service.spec.selector. 이 둘만 TTL 캐시해 하루 약 38만
# 회를 없앤다. 나머지 3회(EndpointSlice / Deployment status / Pod 목록)는 진짜
# 동적이라 캐시하지 않는다.
#
# node_cache(장치 라벨)와 달리 **TTL 이 필요하다**: 노드 라벨은 노드 수명 동안
# 불변이지만, Service 는 나중에 KServe 로 이관될 수 있고 selector 도 라벨을 바꾼
# 재배포로 변할 수 있다.
#
# ⚠️ TTL 이 짧아야 하는 진짜 이유는 호출량이 아니라 **scale-to-zero 각성 안전**
# 이다. ISVC 부재를 캐시하면 network_type 이 그동안 "service" 로 남는데,
# litellm._looks_kserve 는 "이름 규약(-predictor) **또는** network_type==kserve"
# 로 판별한다. 즉 이름 규약을 따르지 않는 Service 에는 ISVC 조회가 **유일한
# KServe 신호**다. 그런 Service 에 Serverless ISVC 가 새로 생기면 TTL 동안
# _deployment_health_safe 가 True 를 돌려주고, LiteLLM /health?model= 이 그
# 백엔드를 ping 해 깨운다(직접 검증: 캐시된 판정 True / 최신 판정 False).
# 이름 규약을 지키면 캐시와 무관하게 막히지만(규약은 ops 보장 사항), 그 보장
# 하나에 각성 방지를 걸어두지 않으려고 TTL 을 60초로 둔다 — 위험 창이 캐시
# 없을 때의 5초(1사이클)에서 12배로만 늘고, 절감은 41만 -> 38만 회로 8% 만
# 준다. 늘리려면 이 절충을 다시 계산할 것.
META_TTL = 60.0
_META_MAX = 4096      # (ns,svc) 단위라 클러스터 Service 수로 이미 유계지만 상한을 둔다


def meta_get(cache, key, now=None):
    """TTL 메타 캐시 조회 -> (hit, value). cache=None 이면 항상 미스(캐시 비활성)."""
    if cache is None:
        return False, None
    ent = cache.get(key)
    if ent is not None and ent[0] > (now if now is not None else time.monotonic()):
        return True, ent[1]
    return False, None


def meta_put(cache, key, value, now=None, ttl=META_TTL):
    if cache is None or ttl <= 0:
        return
    now = now if now is not None else time.monotonic()
    if len(cache) >= _META_MAX:
        for k in [k for k, (e, _) in cache.items() if e <= now]:
            cache.pop(k, None)
        while len(cache) >= _META_MAX:
            cache.pop(next(iter(cache)), None)
    cache[key] = (now + ttl, value)


def meta_drop(cache, key):
    """캐시된 값이 틀렸다는 증거가 나왔을 때 즉시 무효화(TTL 을 기다리지 않는다)."""
    if cache is not None:
        cache.pop(key, None)


def _gpu_qty(v):
    """nvidia.com/gpu 수량 문자열("1","8")을 int 로. 실패하면 0."""
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def _ctr_gpu(ctr):
    """컨테이너 한 개의 nvidia.com/gpu 수 (limits 우선, 없으면 requests)."""
    res = ctr.get("resources") or {}
    q = (res.get("limits") or {}).get(GPU_RESOURCE)
    if q is None:
        q = (res.get("requests") or {}).get(GPU_RESOURCE)
    return _gpu_qty(q)


def _pod_gpu(pod):
    """Pod 한 개가 점유하는 GPU 수 = 컨테이너별 nvidia.com/gpu 합."""
    return sum(_ctr_gpu(ctr)
               for ctr in ((pod.get("spec") or {}).get("containers") or []))


def _pod_engine(pod):
    """서빙 컨테이너의 image/command/args 로 엔진(vllm/sglang) 판별. 미상이면 None.

    GPU 를 점유한 컨테이너를 서빙 컨테이너로 보고 우선 검사한다(queue-proxy/istio
    같은 사이드카 배제 효과). GPU 컨테이너가 없으면 전체 컨테이너를 검사한다.
    이미지가 리네임된 사설 레지스트리라도 command/args('vllm serve',
    'sglang.launch_server')로 폴백 판별된다. 이름 휴리스틱보다 정확하다.
    """
    ctrs = (pod.get("spec") or {}).get("containers") or []
    gpu_ctrs = [c for c in ctrs if _ctr_gpu(c) > 0]
    for ctr in (gpu_ctrs or ctrs):
        blob = " ".join(
            [ctr.get("image") or ""]
            + list(ctr.get("command") or [])
            + list(ctr.get("args") or [])).lower()
        if "sglang" in blob:
            return "sglang"
        if "vllm" in blob:
            return "vllm"
    return None


def _pod_ready(pod):
    """Running + Ready condition True 인 Pod 만 '서빙 중'으로 본다(backends_ready 와 동일 기준)."""
    st = pod.get("status") or {}
    if st.get("phase") != "Running":
        return False
    for cnd in st.get("conditions") or []:
        if cnd.get("type") == "Ready":
            return cnd.get("status") == "True"
    return False


def _short_gpu_product(prod):
    """NVIDIA-H100-80GB-HBM3 -> H100, NVIDIA-B200 -> B200, NVIDIA-A100-SXM4-80GB -> A100."""
    if not prod:
        return None
    s = prod
    if s.upper().startswith("NVIDIA-"):
        s = s[len("NVIDIA-"):]
    return s.split("-")[0] or prod


def _node_gpu_product(client, node_name, cache):
    """노드 라벨 nvidia.com/gpu.product (캐시). 실패/없음이면 None."""
    if not node_name:
        return None
    if node_name in cache:
        return cache[node_name]
    prod = None
    ok, data, _ = client.get("/api/v1/nodes/%s" % node_name)
    if ok:
        labels = (data.get("metadata") or {}).get("labels") or {}
        prod = labels.get(GPU_PRODUCT_LABEL)
        # 성공 응답만 캐시한다 — 캐시가 프로세스 수명(Refresher._node_cache)이 된
        # 뒤로는 일시 실패(타임아웃/429/RBAC 순단)를 캐시하면 그 노드 장치명이
        # 재기동 전까지 'GPU'(미상)로 영구히 굳는다. 실패는 다음 사이클에 재시도.
        # (성공했지만 라벨 없는 노드는 캐시한다 — GFD 미설치 클러스터가 매 사이클
        # 재조회로 돌아가지 않게. 부팅 직후 라벨이 늦게 붙는 노드가 미상으로 남는
        # 트레이드오프는 수용 — 재기동/노드 교체 시 해소.)
        cache[node_name] = prod
    return prod


def selector_key(ns, svc):
    """메타 캐시에서 selector 항목을 가리키는 키(무효화에도 같은 키를 쓴다)."""
    return ("sel", ns, svc)


def service_pod_selector(client, ns, svc, meta_cache=None):
    """Service 의 spec.selector -> Pod labelSelector 문자열. (sel, err).

    Service 이름 자체가 아니라 selector(라벨)로 Pod 을 찾으므로, Service 와
    StatefulSet/Deployment 사이의 네이밍 규칙에 의존하지 않는다.

    meta_cache 를 주면 성공 결과를 TTL 재사용한다 — selector 는 라벨을 바꾼
    재배포 때만 변하는데 매 사이클 Service 오브젝트를 다시 받고 있었다.
    **실패는 캐시하지 않는다**(node 라벨 캐시와 같은 이유: 일시적 RBAC/타임아웃을
    굳히면 그 Service 의 Pod 수가 TTL 동안 '?' 로 고정된다).
    """
    hit, sel = meta_get(meta_cache, selector_key(ns, svc))
    if hit:
        return sel, None
    ok, sdata, serr = client.get(
        "/api/v1/namespaces/%s/services/%s" % (ns, svc))
    if not ok:
        return None, "service: %s" % serr
    seldict = ((sdata.get("spec") or {}).get("selector")) or {}
    if not seldict:
        return None, "service 에 selector 없음"
    sel = ",".join("%s=%s" % (k, v) for k, v in sorted(seldict.items()))
    meta_put(meta_cache, selector_key(ns, svc), sel)
    return sel, None


def collect_gpu_for_service(client, ns, svc, isvc, found, node_cache,
                            meta_cache=None):
    """(ns,svc) 뒤 ready Pod 들의 GPU 수 합 + 장치별 집계 + 서빙 엔진 판별.

    -> {"gpu_ready": int|None, "gpu_products": {short: count}, "gpu_error": str|None,
        "engine": "vllm"|"sglang"|None}
       gpu_ready=None 은 조회 실패(?), 0 은 GPU 없음/scale-to-zero.
       engine 은 이미 받아온 Pod 컨테이너(image/command/args)에서 추가 API 호출
       없이 판별한다 — 미상/Pod 없음(scale-to-zero)이면 None(호출측 휴리스틱 폴백).
       모든 ready Pod 를 보고 서로 다른 엔진이 공존하면(교체 롤아웃 중) "mixed" —
       첫 Pod 하나로 정하면 Pod 목록 순서에 따라 값이 폴링마다 플랩한다.
    Pod 선택: KServe(ISVC found)면 serving.kserve.io/inferenceservice 라벨,
    아니면 Service 의 spec.selector 로 labelSelector 를 만든다.
    """
    out = {"gpu_ready": None, "gpu_products": {}, "gpu_error": None,
           "engine": None}
    if found:
        sel = "serving.kserve.io/inferenceservice=%s" % isvc
    else:
        sel, serr = service_pod_selector(client, ns, svc, meta_cache)
        if sel is None:
            out["gpu_error"] = serr
            return out
    ok, data, err = client.get(
        "/api/v1/namespaces/%s/pods?labelSelector=%s"
        % (ns, urllib.parse.quote(sel, safe="=,")))
    if not ok:
        out["gpu_error"] = "pods: %s" % err
        return out
    if not found and not (data.get("items") or []):
        # 캐시한 selector 로 Pod 이 0건 — 정말 0 replica 일 수도 있지만 라벨이
        # 바뀐 재배포일 수도 있다. TTL 을 기다리지 않고 버려 다음 사이클에 다시
        # 읽는다(자기치유). 손해는 실제로 0 replica 인 Service 가 매 사이클
        # selector 를 다시 받는 것뿐 — 그쪽은 어차피 예산이 남는다.
        meta_drop(meta_cache, selector_key(ns, svc))
    total = 0
    products = {}
    any_ready = False
    engines = set()
    for pod in data.get("items") or []:
        if not _pod_ready(pod):
            continue
        any_ready = True
        eng = _pod_engine(pod)
        if eng:
            engines.add(eng)
        g = _pod_gpu(pod)
        if g <= 0:
            continue
        total += g
        prod = _short_gpu_product(_node_gpu_product(
            client, (pod.get("spec") or {}).get("nodeName"), node_cache)) or "GPU"
        products[prod] = products.get(prod, 0) + g
    out["gpu_ready"] = total if any_ready else 0
    out["gpu_products"] = products
    if engines:
        out["engine"] = engines.pop() if len(engines) == 1 else "mixed"
    return out
