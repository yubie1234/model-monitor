"""Backend 개수: api_base(LB) 뒤의 실제 Pod/replica 수.

우선순위 체인:
  KServe ISVC -> Deployment 라벨 합산
  -> EndpointSlice(ready 주소 수) -> Knative PodAutoscaler actualScale
  -> RawDeployment Deployment -> StatefulSet(selector->owner 로 desired 보강) -> none
값을 모르면 '?' 로 두고 절대 지어내지 않는다. 실패는 k8s_error 로 기록한다.
"""

import urllib.parse

from app.services.gpu import (collect_gpu_for_service, meta_drop, meta_get,
                              meta_put, selector_key, service_pod_selector)


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


def detect_mode_and_revision(client, ns, svc, meta_cache=None):
    """service 이름에서 ISVC 추정 -> deploymentMode + revision + found 여부.

    meta_cache 를 주면 **부재(HTTP 404)만** TTL 캐시한다. 일반 Service 는 이
    조회가 매 사이클 404 를 받으므로(하루 17,280회/Service) 그것만 없애도 크다.

    성공(found=True)은 캐시하지 않는다 — 반환하는 revision 이
    status.components.predictor.latestReadyRevision 에서 오는 **동적** 값이라
    캐시하면 롤아웃 후에도 옛 revision 이 굳고, 그걸 쓰는 Knative
    PodAutoscaler 조회가 사라진 revision 을 가리킨다.

    404 가 아닌 실패(RBAC/타임아웃/프록시)도 캐시하지 않는다 — 일시적 실패를
    굳히면 network_type 이 TTL 동안 '-' 로 고정된다(node 라벨 캐시와 같은 원칙).

    ⚠️ 부재를 캐시하면 network_type 이 TTL 동안 'service' 로 남는다. 이름 규약
    (-predictor)을 따르지 않는 Service 에는 이 조회가 유일한 KServe 신호라,
    그런 Service 에 Serverless ISVC 가 새로 생기면 TTL 동안 health check 대상에
    들어가 백엔드를 깨울 수 있다. 그래서 META_TTL 은 짧게(60s) 잡혀 있다 —
    자세한 절충은 gpu.META_TTL 주석 참고. TTL 을 늘리려면 그 계산을 다시 할 것.
    """
    isvc = svc
    for suffix in ("-predictor", "-transformer", "-explainer"):
        if isvc.endswith(suffix):
            isvc = isvc[: -len(suffix)]
            break
    absent = {"mode": "Unknown", "revision": None, "isvc": isvc, "found": False}
    hit, cached_err = meta_get(meta_cache, ("isvc-absent", ns, isvc))
    if hit:
        return dict(absent), cached_err
    ok, data, err = client.get(
        "/apis/serving.kserve.io/v1beta1/namespaces/%s/inferenceservices/%s"
        % (ns, isvc))
    if not ok:
        # 호출측이 "HTTP 404" 접두사로 '없음' 과 '판정 불가' 를 가르므로 err 도
        # 함께 캐시해 캐시 히트가 원본과 똑같이 분기되게 한다.
        if str(err or "").startswith("HTTP 404"):
            meta_put(meta_cache, ("isvc-absent", ns, isvc), err)
        return dict(absent), err
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


def count_desired_via_selector(client, ns, svc, meta_cache=None):
    """네이밍 독립적 desired 보강: Service selector -> Pod ownerReferences -> StatefulSet.

    Service 와 StatefulSet/Pod 사이엔 정해진 네이밍 규칙이 없어 같은 이름으로 못
    찾는다. 대신 k8s 의 실제 소유 관계를 따라간다:
      Service.spec.selector 로 Pod 나열 -> Pod.ownerReferences 의 StatefulSet 수집
      -> 각 StatefulSet.spec.replicas 합산(= desired).
    StatefulSet 은 Pod 을 직접 소유하므로 한 홉이면 된다. 소유 StatefulSet 이
    없거나 조회 실패면 (None, err) — desired 는 지어내지 않는다.
    """
    sel, serr = service_pod_selector(client, ns, svc, meta_cache)
    if sel is None:
        return None, serr
    ok, data, err = client.get(
        "/api/v1/namespaces/%s/pods?labelSelector=%s"
        % (ns, urllib.parse.quote(sel, safe="=,")))
    if not ok:
        return None, "pods: %s" % err
    if not (data.get("items") or []):
        # 캐시한 selector 가 Pod 을 못 찾았다 — 라벨이 바뀐 재배포일 수 있으니
        # 버리고 다음 사이클에 다시 읽는다(collect_gpu_for_service 와 같은 규칙).
        meta_drop(meta_cache, selector_key(ns, svc))
    sts_names = set()
    for pod in data.get("items") or []:
        for ref in ((pod.get("metadata") or {}).get("ownerReferences") or []):
            if ref.get("kind") == "StatefulSet" and ref.get("name"):
                sts_names.add(ref["name"])
    if not sts_names:
        return None, "소유 StatefulSet 없음"
    desired = 0
    found = False
    for name in sorted(sts_names):
        ok, sdata, _ = client.get(
            "/apis/apps/v1/namespaces/%s/statefulsets/%s" % (ns, name))
        if ok:
            r = _int_or_none((sdata.get("spec") or {}).get("replicas"))
            if r is not None:
                desired += r
                found = True
    if not found:
        return None, "statefulset spec.replicas 미상"
    return {"desired": desired, "source": "statefulset"}, None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def resolve_backend_count(deployment, client, settings, cache=None,
                          node_cache=None, meta_cache=None):
    """우선순위 체인으로 한 deployment 의 LB 뒤 backend 개수 산출 -> 필드 dict.

    cache={(ns,svc): out} 를 주면 같은 Service 를 가리키는 여러 model_name 이
    한 스냅샷 빌드 안에서 k8s API 를 중복 조회하지 않고 결과를 재사용한다.

    node_cache={node_name: gpu_product} 를 주면 노드 GPU 장치명 라벨 조회를
    스냅샷 주기를 넘어 재사용한다(라벨은 노드 수명 동안 불변). 미지정이면
    client 수명(=한 사이클) 캐시로 폴백한다.
    """
    out = {"backend_pods": [], "backends_ready": None, "backends_desired": None,
           "backend_source": "none", "mode": "Unknown",
           "scale_to_zero": False, "namespace": None, "service": None,
           "network_type": "-",     # kserve | service | external | '-'(판정 불가)
           "network_type_error": None,   # '-' 일 때 ISVC 조회 실패 원인
           # Knative 판정(mode 문자열 또는 revision 존재)·activator-only 증거를
           # 명시 필드로 내보낸다 — 선택적 health check(_deployment_health_safe)가
           # 문자열 재추론 없이 단일 판정을 쓰게(두 정의가 드리프트하지 않게).
           "serverless": False, "activator_only": False,
           "k8s_error": None,
           "gpu_ready": None, "gpu_products": {}, "gpu_error": None}
    api_base = deployment.get("api_base")
    if not api_base:
        return out

    parsed = parse_api_base(api_base, client.default_namespace,
                            settings.get("namespace_overrides"))
    out["namespace"] = parsed["namespace"]
    out["service"] = parsed["service"]
    if parsed["kind"] != "k8s-svc" or not parsed["service"]:
        out["backend_source"] = "external"
        out["network_type"] = "external"
        return out

    if not client.enabled:
        return out

    ns, svc = parsed["namespace"], parsed["service"]
    if cache is not None and (ns, svc) in cache:
        return dict(cache[(ns, svc)])   # 같은 Service 는 재조회 생략
    activator_ns = settings.get("activator_namespace", "knative-serving")
    errors = []

    info, isvc_err = detect_mode_and_revision(client, ns, svc, meta_cache)
    out["mode"] = info["mode"]
    isvc, revision = info["isvc"], info["revision"]
    serverless = _is_serverless(info["mode"], revision)
    out["serverless"] = serverless

    # 네트워크 타입 — 문자열 추측이 아니라 k8s 사실로 판정한다.
    # ISVC 조회 성공 = KServe 기반. HTTP 404 = ISVC 없음(단순 Service. CRD 미설치도
    # 404 인데, KServe 가 없는 클러스터면 service 가 맞다). 그 외 실패(RBAC/타임아웃
    # /프록시 오류)는 판정 불가('-')로 두고 network_type_error 에 원인을 남긴다 —
    # 잘못된 확신보다 미상이 낫다(개수의 '?' 정책과 동일).
    # 판정은 "HTTP 404" 접두사(K8sClient.get 의 HTTPError 포맷)로만 — 부분문자열
    # 매칭은 'char 404' 같은 우연 일치로 오판한다.
    # 한계: ISVC 이름 추측(-predictor 등 접미사 제거)이 빗나가는 네이밍이면 KServe
    # 여도 404 → service 로 분류될 수 있다.
    if info["found"]:
        out["network_type"] = "kserve"
    elif isvc_err and not isvc_err.startswith("HTTP 404"):
        # 개수 수집이 성공하면 k8s_error 가 비므로, '-' 의 원인은 전용 필드에 보존.
        out["network_type_error"] = "isvc: %s" % isvc_err
        errors.append("isvc: %s" % isvc_err)
    else:
        out["network_type"] = "service"

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
            # scale-to-zero 된 Knative Service 의 결정적 증거 — 에러 문자열로만
            # 남기지 않고 명시 필드로 내보낸다(능동 health check 가 이걸 보고
            # 절대 ping 하지 않게. ping 은 activator 를 거쳐 백엔드를 깨운다).
            out["activator_only"] = True
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
        # Deployment 가 없으면(StatefulSet 으로 뜬 경우) desired 가 여전히 null 이다.
        # 이름으로는 못 찾으므로(Service↔STS 네이밍 규칙 없음) selector -> Pod ->
        # ownerReferences 로 소유 StatefulSet 을 찾아 desired 를 보강한다. 이게 없으면
        # EndpointSlice ready 만 잡혀 집계에서 ready 합 > desired 합(=100% 초과)이 된다.
        if out["backends_desired"] is None:
            own, oerr = count_desired_via_selector(client, ns, svc,
                                                   meta_cache)
            if own is not None:
                out["backends_desired"] = own.get("desired")
            elif oerr:
                errors.append("desired(selector): %s" % oerr)

    if out["backends_ready"] is None and errors:
        out["k8s_error"] = "; ".join(errors)

    # GPU 개수 + 장치명 (기본 ON; gpu_info=False 면 건너뜀).
    # 부하(load) 수집이 켜져 있으면 gpu_info 가 꺼져 있어도 이 블록을 돈다 —
    # 같은 Pod 목록 한 번으로 GPU 집계와 Pod 주소(pod_targets)를 둘 다 얻기 때문에
    # k8s 호출이 늘지 않는다.
    # 한 건 실패가 전체를 막지 않게 try/except -> gpu_ready=None(=?) 폴백.
    if settings.get("gpu_info") or settings.get("load"):
        try:
            # 노드 GPU 장치명 라벨(nvidia.com/gpu.product)은 노드 수명 동안 불변이라
            # 매 사이클 Node 오브젝트(status.images 포함 수십 KB)를 다시 받을 이유가
            # 없다. 호출측이 사이클 간 유지되는 node_cache 를 넘기면 그걸 쓰고(리프레셔
            # 경로), 없으면(직접 호출/테스트) 종전처럼 client 수명(1사이클) 캐시로 폴백.
            nc = node_cache
            if nc is None:
                nc = getattr(client, "_node_cache", None)
                if nc is None:
                    nc = {}
                    setattr(client, "_node_cache", nc)
            g = collect_gpu_for_service(
                client, ns, svc, isvc, info["found"], nc, meta_cache)
            if settings.get("gpu_info"):
                out["gpu_ready"] = g["gpu_ready"]
                out["gpu_products"] = g["gpu_products"]
                out["gpu_error"] = g["gpu_error"]
            # Pod 주소는 부하 수집(load)이 Pod 별 /metrics 를 읽는 데 쓴다.
            out["backend_pods"] = g.get("pod_targets") or []
            # Pod 컨테이너(이미지/커맨드) 기반 엔진 판정 — 이름 휴리스틱보다
            # 정확하므로 있으면 litellm 쪽 backend_type 을 덮어쓴다.
            if g.get("engine"):
                out["backend_type"] = g["engine"]
                out["backend_type_source"] = "pod"
        except Exception as e:  # noqa: BLE001
            out["gpu_error"] = "%s: %s" % (type(e).__name__, e)

    if cache is not None:
        cache[(ns, svc)] = dict(out)
    return out
