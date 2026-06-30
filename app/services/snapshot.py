"""스냅샷 파이프라인: 수집 -> 정규화 -> 집계.

build_snapshot(settings) -> snap dict 가 API/대시보드/JSON 이 똑같이 소비하는 단일
산출물이다. 렌더러가 아니라 이 스냅샷을 바꿔서 모든 출력을 동기화한다.
"""

from datetime import datetime

from app import __version__
from app.core.k8s import K8sClient
from app.services.backend_count import resolve_backend_count
from app.services.litellm import (
    collect_backend,
    collect_litellm,
    discover_backends,
    _strip_openai_suffix,
)


def build_snapshot(settings, with_health=True):
    """전체 수집 -> 스냅샷 dict.

    with_health=False 면 느린 /health 를 건너뛴다(웹은 health 를 별도로 주입).
    """
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "litellm": None,
        "backends": [],
        "summary": {},
    }

    if settings.get("litellm_url"):
        snap["litellm"] = collect_litellm(
            settings["litellm_url"], settings.get("api_key"), settings["timeout"],
            settings.get("health_timeout"), with_health=with_health
        )

    if settings.get("probe_backends"):
        targets = settings.get("backends") or []
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
    # (API/웹/JSON 모두 동일한 status 를 쓰도록 여기서 한 번에 적용)
    if snap["litellm"]:
        snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])

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
    # model_name 기준으로 정렬해 표시 순서를 안정화한다(API/웹/JSON 공통).
    # 동률(대소문자만 다른 이름 'vllm-X'↔'vLLM-X', 또는 같은 이름의 deployment 가
    # 여러 개)일 때 입력 순서를 따라가면 폴링마다 순서가 뒤바뀐다. 그래서 lower 뒤로
    # 원문 이름 → api_base → id 를 결정적 tiebreaker 로 둬 전순서를 고정한다.
    merged.sort(key=lambda x: (str(x.get("model_name") or "").lower(),
                               str(x.get("model_name") or ""),
                               str(x.get("api_base") or ""),
                               str(x.get("id") or "")))
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
        "gpu_total": 0,              # 모든 backend 의 ready GPU 합 (Service dedup)
        "gpu_products": {},          # {장치명: 개수}
        "gpu_known": False,
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

        # LB 뒤 backend Pod 집계 (값이 있는 deployment 만).
        # 여러 model_name 이 같은 백엔드 Service 를 공유할 수 있으므로
        # (namespace, service) 유일 기준으로 한 번만 더한다 — 안 그러면
        # 공유 Service 의 물리 Pod 가 model_name 수만큼 이중 집계된다.
        # service 식별이 안 되면(external 등) api_base 로 폴백해 그래도 dedup.
        seen_svc = set()
        seen_gpu = set()
        for d in ll.get("deployments") or []:
            key = (d.get("namespace"), d.get("service"))
            if key == (None, None):
                key = ("", d.get("api_base"))
            if d.get("backends_ready") is not None and key not in seen_svc:
                seen_svc.add(key)
                s["backend_pods_ready"] += d["backends_ready"]
                s["backend_pods_known"] = True
                if d.get("backends_desired") is not None:
                    s["backend_pods_desired"] += d["backends_desired"]
            # GPU 도 (ns,svc) 기준 dedup — 공유 백엔드의 물리 GPU 이중 집계 방지.
            if d.get("gpu_ready") is not None and key not in seen_gpu:
                seen_gpu.add(key)
                s["gpu_total"] += d["gpu_ready"]
                s["gpu_known"] = True
                for prod, n in (d.get("gpu_products") or {}).items():
                    s["gpu_products"][prod] = s["gpu_products"].get(prod, 0) + n

    backends = snap.get("backends") or []
    s["backends_total"] = len(backends)
    s["backends_up"] = sum(1 for b in backends if b.get("up"))
    s["backend_models"] = sum(len(b.get("models") or []) for b in backends)
    return s
