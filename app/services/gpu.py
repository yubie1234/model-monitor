"""GPU: backend Pod 의 nvidia.com/gpu 개수 + 장치 모델명(H100/B200 ...).

개수 -> Pod spec resources.limits["nvidia.com/gpu"]
장치 -> Pod 가 뜬 노드의 라벨 nvidia.com/gpu.product (GPU Operator/GFD)
멀티노드 GPU 환경 없음 전제: Pod 1개 = 노드 1개.
"""

import urllib.parse

GPU_RESOURCE = "nvidia.com/gpu"
GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"


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
    cache[node_name] = prod
    return prod


def collect_gpu_for_service(client, ns, svc, isvc, found, node_cache):
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
        ok, sdata, serr = client.get(
            "/api/v1/namespaces/%s/services/%s" % (ns, svc))
        if not ok:
            out["gpu_error"] = "service: %s" % serr
            return out
        seldict = ((sdata.get("spec") or {}).get("selector")) or {}
        if not seldict:
            out["gpu_error"] = "service 에 selector 없음"
            return out
        sel = ",".join("%s=%s" % (k, v) for k, v in sorted(seldict.items()))
    ok, data, err = client.get(
        "/api/v1/namespaces/%s/pods?labelSelector=%s"
        % (ns, urllib.parse.quote(sel, safe="=,")))
    if not ok:
        out["gpu_error"] = "pods: %s" % err
        return out
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
