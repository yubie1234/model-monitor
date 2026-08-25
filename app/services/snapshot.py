"""스냅샷 파이프라인: 수집 -> 정규화 -> 집계.

build_snapshot(settings) -> snap dict 가 API/대시보드/JSON 이 똑같이 소비하는 단일
산출물이다. 렌더러가 아니라 이 스냅샷을 바꿔서 모든 출력을 동기화한다.
"""

import time
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


def build_snapshot(settings, with_health=True, node_cache=None):
    """전체 수집 -> 스냅샷 dict.

    with_health=False 면 느린 /health 를 건너뛴다(웹은 health 를 별도로 주입).
    node_cache 를 주면 노드 GPU 장치명 라벨을 사이클 간 재사용한다(불변 라벨을
    5초마다 다시 받지 않게 — 리프레셔가 프로세스 수명 캐시를 넘긴다).
    """
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 스냅샷 신선도(staleness) 판정용 epoch. ts 는 사람용 로컬시간 문자열이라
        # TZ·파싱이 필요하지만 ts_epoch 는 Prometheus 가 snapshot age 를 그대로
        # 계산할 수 있게 한다(model_monitor_snapshot_timestamp_seconds).
        "ts_epoch": time.time(),
        "litellm": None,
        "backends": [],
        "summary": {},
    }

    if settings.get("litellm_url"):
        snap["litellm"] = collect_litellm(
            settings["litellm_url"], settings.get("api_key"), settings["timeout"],
            settings.get("health_timeout"), with_health=with_health
        )

    # 각 deployment 의 LB(api_base) 뒤 backend Pod 개수 채우기
    client = K8sClient.from_settings(settings)
    snap["backend_count_enabled"] = bool(client)
    if client and snap["litellm"]:
        bc_cache = {}  # (ns,svc) -> 결과: 같은 Service 중복 k8s 조회 방지
        for d in snap["litellm"].get("deployments") or []:
            try:
                d.update(resolve_backend_count(
                    d, client, settings, bc_cache, node_cache))
            except Exception as e:  # noqa: BLE001  (한 건 실패가 전체를 막지 않게)
                d["k8s_error"] = "%s: %s" % (type(e).__name__, e)

    # 백엔드 직접 probe — backend_count **뒤**에 실행한다: 직접 probe 는 LiteLLM
    # 을 경유하지 않고 백엔드에 바로 닿으므로 Serverless(scale-to-zero)를 깨우는데,
    # k8s 판정(serverless/scale_to_zero/mode)이 붙은 뒤라야 discover_backends 가
    # 위험 백엔드를 안전 필터로 제외할 수 있다. 수동 backends 목록은 운영자의
    # 명시 선택이므로 필터하지 않는다.
    if settings.get("probe_backends"):
        targets = settings.get("backends") or []
        # 수동 목록이 없으면 LiteLLM /model/info + /health 에서 자동 발견.
        if not targets and snap["litellm"]:
            targets = discover_backends(snap["litellm"])
        for b in targets:
            snap["backends"].append(collect_backend(b, settings["timeout"]))

    # /health 상태 + backend readiness 를 합쳐 deployment 에 status 부여
    # (API/웹/JSON 모두 동일한 status 를 쓰도록 여기서 한 번에 적용)
    if snap["litellm"]:
        snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])

    snap["summary"] = summarize(snap)
    return snap


# DOWN 사유(down_reason) 정규화 카테고리 — 대시보드 그룹핑/필터, 그리고 (원한다면)
# Prometheus 라벨로도 안전하게 쓸 수 있게 소수의 고정 집합으로 제한한다. LiteLLM
# error 원문은 버전마다 표현이 달라 문자열 그대로는 카디널리티가 폭발하므로,
# 키워드 → 카테고리로 접는다. exception_status(HTTP 코드)는 보조 신호.
_REASON_KEYWORDS = [
    ("connection", ("connection error", "getaddrinfo", "cannot connect",
                    "connection refused", "connect call failed",
                    "name or service not known", "no route to host")),
    ("timeout", ("timeout", "timed out")),
    ("rate_limit", ("rate limit", "ratelimit", "too many requests")),
    ("auth", ("authenticationerror", "unauthorized", "invalid api key",
              "permission")),
    ("not_found", ("not found", "notfounderror", "no healthy",
                   "does not exist")),
    ("context_window", ("context window", "context length", "maximum context")),
]


def _classify_health_error(ep):
    """/health unhealthy endpoint 의 error/exception_status 를 (down_reason,
    status_detail) 로 정규화한다.

    - down_reason: _REASON_KEYWORDS 의 소수 카테고리(없으면 HTTP 코드 버킷 →
      server_error/client_error, 그래도 없으면 'other').
    - status_detail: error 첫 줄만(스택트레이스 제거) 200자 캡. 운영자가 대시보드
      툴팁에서 '왜 DOWN 인지'를 raw JSON 없이 바로 읽게 한다.
    """
    err = ep.get("error")
    err = "" if err is None else str(err)
    # 첫 줄만 — LiteLLM 은 'litellm.X: ... .\nstack trace: Traceback ...' 처럼
    # 메시지 뒤에 스택트레이스를 개행으로 붙여 보낸다. 첫 줄이 사람이 읽을 요약.
    detail = err.split("\n", 1)[0].strip()
    if len(detail) > 200:
        detail = detail[:197] + "..."
    low = err.lower()
    reason = None
    for name, kws in _REASON_KEYWORDS:
        if any(kw in low for kw in kws):
            reason = name
            break
    if reason is None:
        # HTTP 코드 버킷 폴백(문자/정수 모두 수용).
        code = ep.get("exception_status")
        try:
            code = int(str(code).strip())
        except (TypeError, ValueError):
            code = None
        if code is not None:
            if 500 <= code <= 599:
                reason = "server_error"
            elif 400 <= code <= 499:
                reason = "client_error"
        if reason is None:
            reason = "other" if (detail or code is not None) else "unknown"
    return reason, (detail or None)


def merge_deployments_with_health(ll):
    """/model/info(api_base) 와 /health(상태)를 api_base 기준으로 합친 뷰."""
    health = ll.get("health") or {}
    healthy = {_strip_openai_suffix(ep["api_base"])
               for ep in (health.get("healthy_endpoints") or [])
               if ep.get("api_base")}
    # DOWN 행에 사유를 실어 주기 위해 unhealthy 는 base -> endpoint 맵으로 둔다
    # (기존엔 base 집합만 만들어 error 문자열을 통째로 버렸다). 같은 base 가
    # 여러 번 오면 첫 항목을 유지한다(aggregate 단계에서 이미 dedup 됨).
    unhealthy = {}
    for ep in (health.get("unhealthy_endpoints") or []):
        if ep.get("api_base"):
            unhealthy.setdefault(_strip_openai_suffix(ep["api_base"]), ep)
    merged = []
    for d in ll.get("deployments") or []:
        base = _strip_openai_suffix(d["api_base"]) if d.get("api_base") else None
        reason = detail = None
        if base in healthy:
            status, src = "UP", "health"
        elif base in unhealthy:
            status, src = "DOWN", "health"
            reason, detail = _classify_health_error(unhealthy[base])
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
                    # k8s 로 DOWN 판정 = ready Pod 0. /health error 는 없지만
                    # 사유는 명확하므로 동일 필드로 표면화한다.
                    reason, detail = "no_ready_pods", "ready Pod 없음 (0개)"
            else:
                status, src = "?", "unknown"
        row = {**d, "status": status, "status_source": src}
        # DOWN 일 때만 사유 필드를 부착한다(UP/'?'엔 불필요). down_reason/status_detail
        # 은 per-user 리댁션 allowlist 밖이라 비-admin 뷰에선 자동 미노출(fail-safe).
        # 아래 PAUSED 승격보다 **먼저** 붙인다 — 일시중지된 행에도 원래 health 가
        # DOWN 이었다면 그 사유를 남겨야 한다(다시 켰을 때 실제로 뜰지 판단용).
        if reason is not None:
            row["down_reason"] = reason
        if detail is not None:
            row["status_detail"] = detail
        # 이 함수는 한 스냅샷에서 **두 번** 돈다(build_snapshot 에서 health 없이
        # 한 번, state.Refresher 가 /health 를 주입한 뒤 한 번). 이전 회차가 남긴
        # health_status* 를 먼저 지워야 blocked 가 풀린 행에 옛 값이 남지 않는다.
        row.pop("health_status", None)
        row.pop("health_status_source", None)
        # 관리자 일시중지(LiteLLM model_info.blocked)는 health 결과를 덮어쓴다.
        # LiteLLM /health 는 blocked 백엔드도 그대로 ping 해서 healthy 로 보고
        # 하지만(v1.90.0), 라우팅 풀에서는 빠져 있어 트래픽을 전혀 못 받는다.
        # 그대로 UP 으로 두면 "정상인데 아무도 못 쓰는" 거짓 정상이 된다.
        # 원래 health 판정은 health_status 로 남긴다 — 운영자가 다시 켰을 때
        # 실제로 뜰 백엔드인지(Pod 가 살아있는지) 알아야 하기 때문.
        #
        # 그 판정의 **근거**(health_status_source)도 함께 남긴다. status_source 는
        # "blocked" 로 덮이므로 이걸 안 남기면 PAUSED(UP) 의 UP 이 /health 실측인지
        # k8s readiness 추정인지 구분이 사라진다 — MONITOR_HEALTH 기본값이 off 라
        # 실제로는 추정값인 경우가 더 흔한데, 화면엔 같은 확신으로 보이게 된다.
        if d.get("blocked") is True:
            row["health_status"] = status
            row["health_status_source"] = src
            row["status"], row["status_source"] = "PAUSED", "blocked"
        merged.append(row)
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
        "deployments_blocked": 0,    # 관리자 일시중지(PAUSED) deployment 수
        "blocked_known": False,      # LiteLLM 이 blocked 를 알려주면 True
        "backends_up": 0,
        "backends_total": 0,
        "backend_models": 0,
        "backend_pods_ready": 0,     # 모든 LB 뒤 ready Pod 합계
        "backend_pods_desired": 0,   # 목표 replica 합계
        "backend_pods_known": False,
        "gpu_total": 0,              # 모든 backend 의 ready GPU 합 (Service dedup)
        "gpu_products": {},          # {장치명: 개수}
        "gpu_known": False,
        "k8s_errors": 0,             # backend Pod 수 수집 실패 deployment 수
        "gpu_errors": 0,             # GPU 정보 수집 실패 deployment 수
    }
    ll = snap.get("litellm")
    if ll:
        s["model_groups"] = len(ll.get("groups") or [])
        deps = ll.get("deployments") or []
        s["deployments_registered"] = len(deps)
        health = ll.get("health") or {}
        # 수집 실패 총계 — 셀별 ⚠ 툴팁만으론 '얼마나 광범위하게 깨졌는지'가 안 보여
        # 상단 배너/메트릭이 읽을 총계를 여기서 만든다(추가 수집 없이 재조합).
        s["k8s_errors"] = sum(1 for d in deps if d.get("k8s_error"))
        s["gpu_errors"] = sum(1 for d in deps if d.get("gpu_error"))

        # 카드 수치를 표(merge_deployments_with_health 의 per-row status)와 항상
        # 일치시킨다. deployment 가 있으면 merged status 로 집계(=/health 가
        # 타임아웃해도 k8s readiness 보정이 반영됨), 없으면 /health 카운트로 폴백.
        # 일시중지(PAUSED)는 UP 도 DOWN 도 아닌 제3의 상태 — healthy/unhealthy
        # 어느 쪽에도 안 들어간다. 장애 카드에 섞이면 안 되고(의도된 정지),
        # 정상 카드에 섞여도 안 된다(트래픽을 못 받으니 가용 용량이 아님).
        # 카드 = 표 항등식은 그대로 유지된다: 양쪽 다 같은 per-row status 를 센다.
        s["blocked_known"] = any("blocked" in d for d in deps)
        s["deployments_blocked"] = sum(1 for d in deps if d.get("status") == "PAUSED")
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
        # per-user 리댁션 뷰는 ns/svc/api_base 가 모두 없으므로 익명 backend_ref 로
        # dedup 한다 — 없으면 모든 행이 같은 키로 붕괴해 첫 행만 집계되는 버그.
        seen_svc = set()
        seen_gpu = set()
        for d in ll.get("deployments") or []:
            key = (d.get("namespace"), d.get("service"))
            if key == (None, None):
                key = ("", d.get("backend_ref") or d.get("api_base"))
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
