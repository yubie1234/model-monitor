"""Prometheus 메트릭 (text exposition format 0.0.4).

수집은 하지 않는다 — 이미 만들어진 스냅샷을 문자열로 포맷만 한다(요청 경로 비차단).
상태 인코딩: UP=1, DOWN=0, ?(미상/scale-to-zero idle/PAUSED)=-1.
PAUSED(관리자 일시중지)는 model_monitor_model_blocked 로 따로 노출한다 — 기존
알림(model_up==0)을 건드리지 않으면서 "꺼둔 모델 제외" 를 표현할 수 있게.
카디널리티: 라벨은 model/namespace/service/backend_source/status_source 로 한정하고
api_base(내부 URL)는 노출하지 않는다(per-user 뷰에서 숨기는 내부 정보).
"""

import time

from app import __version__

# 상태 -> 게이지 값. UP=1, DOWN=0, 그 외(?/unknown/scale-to-zero idle)=-1.
_STATUS_GAUGE = {"UP": 1, "DOWN": 0}


def _prom_label(v):
    """Prometheus 라벨 값 이스케이프(역슬래시/따옴표/개행)."""
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _prom_num(v):
    """게이지 값 직렬화. bool -> 0/1, 정수형 float -> 정수, 그 외 그대로."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _dedup_samples(samples, reduce_fn):
    """같은 라벨 셋의 중복 series 를 reduce_fn 으로 합쳐 valid exposition 을 보장한다.

    LiteLLM 은 한 model_name 에 여러 deployment 를 둘 수 있고(=로드밸런싱), 그러면
    동일 라벨 series 가 중복돼 Prometheus 스크레이프가 깨진다. 값이 None 인 샘플은 버린다.
    """
    acc, order = {}, []
    for labels, value in samples:
        if value is None:
            continue
        key = tuple(sorted(labels.items()))
        if key not in acc:
            acc[key] = [labels, value]
            order.append(key)
        else:
            acc[key][1] = reduce_fn(acc[key][1], value)
    return [(acc[k][0], acc[k][1]) for k in order]


def _status_reduce(a, b):
    """같은 라벨의 상태 충돌 시: DOWN(0) 우선, 다음 UP(1), 그다음 미상(-1)."""
    if 0 in (a, b):
        return 0
    if 1 in (a, b):
        return 1
    return -1


def render_prometheus_metrics(snap):
    """캐시된 스냅샷을 Prometheus text exposition(0.0.4) 으로 변환한다.

    수집은 하지 않는다 — 이미 만들어진 snap 을 문자열로 포맷만 한다(요청 경로 비차단).
    상태 인코딩: UP=1, DOWN=0, ?(미상/scale-to-zero idle)=-1.
    """
    lines = []

    def emit(name, help_text, mtype, samples):
        lines.append("# HELP %s %s" % (name, help_text))
        lines.append("# TYPE %s %s" % (name, mtype))
        for labels, value in samples:
            if value is None:
                continue
            if labels:
                lbl = ",".join('%s="%s"' % (k, _prom_label(val))
                               for k, val in labels.items())
                lines.append("%s{%s} %s" % (name, lbl, _prom_num(value)))
            else:
                lines.append("%s %s" % (name, _prom_num(value)))

    s = snap.get("summary") or {}
    ll = snap.get("litellm") or {}
    deps = ll.get("deployments") or []

    # --- 모니터 자체 메타(스크레이프 신뢰도) ---
    emit("model_monitor_up",
         "모니터가 스냅샷을 갖고 응답 중이면 1, 첫 수집 전(loading)이면 0.", "gauge",
         [({}, 0 if snap.get("loading") else 1)])
    emit("model_monitor_build_info",
         "버전 정보. 값은 항상 1, version 라벨로 식별.", "gauge",
         [({"version": snap.get("version") or __version__}, 1)])
    emit("model_monitor_backend_count_enabled",
         "k8s 백엔드 Pod 수 수집이 켜져 있으면 1.", "gauge",
         [({}, 1 if snap.get("backend_count_enabled") else 0)])
    # 백그라운드 수집이 실패해도 store 는 직전 스냅샷을 계속 서빙한다(model_monitor_up
    # 은 그래도 1). 그래서 '마지막 수집이 실패 중'을 별도 신호로 노출한다.
    emit("model_monitor_collect_failing",
         "마지막 백그라운드 수집이 실패해 직전 스냅샷을 서빙 중이면 1.", "gauge",
         [({}, 1 if snap.get("collect_error") else 0)])
    # LiteLLM 은 최상류 단일 의존성 — 죽으면 deployment 시리즈가 통째로 사라질 뿐
    # (0 이 아니라 부재)이라 알림이 안 걸린다. reachable 을 명시적으로 노출한다.
    emit("model_monitor_litellm_reachable",
         "LiteLLM 게이트웨이에 도달 가능하면 1(미도달/미설정이면 0).", "gauge",
         [({}, 1 if ll.get("reachable") else 0)])
    emit("model_monitor_litellm_errors",
         "LiteLLM 수집 중 기록된 에러 문자열 수(도달성/선택적 health 경고 포함).",
         "gauge",
         [({}, len(ll.get("errors") or []))])
    emit("model_monitor_collect_errors",
         "k8s 조회 에러가 기록된 deployment 수(>0 이면 일부 Pod 수가 부정확).", "gauge",
         [({}, s.get("k8s_errors", sum(1 for d in deps if d.get("k8s_error"))))])
    emit("model_monitor_gpu_collect_errors",
         "GPU 정보 조회 에러가 기록된 deployment 수(>0 이면 일부 GPU 수가 부정확).",
         "gauge",
         [({}, s.get("gpu_errors", sum(1 for d in deps if d.get("gpu_error"))))])
    # 스냅샷 신선도: ts_epoch 를 그대로 노출하고 age 도 계산해 준다. Refresher 가
    # 멈추면(model_monitor_up=1 인데도) age 가 계속 커진다 → SnapshotStale 알림.
    ts_epoch = snap.get("ts_epoch")
    if isinstance(ts_epoch, (int, float)):
        emit("model_monitor_snapshot_timestamp_seconds",
             "마지막 스냅샷을 만든 시각(unix epoch 초).", "gauge",
             [({}, round(float(ts_epoch), 3))])
        emit("model_monitor_snapshot_age_seconds",
             "마지막 스냅샷 이후 경과 초(렌더 시점 기준). 계속 커지면 수집 멈춤.",
             "gauge",
             [({}, max(0.0, round(time.time() - float(ts_epoch), 1)))])

    # --- 요약(summary) 게이지 ---
    emit("model_monitor_deployments_total",
         "집계된 deployment(모델) 총 수.", "gauge",
         [({}, s.get("deployments_total", 0))])
    emit("model_monitor_deployments_healthy",
         "상태 UP 인 deployment 수.", "gauge",
         [({}, s.get("deployments_healthy", 0))])
    emit("model_monitor_deployments_unhealthy",
         "상태 DOWN 인 deployment 수.", "gauge",
         [({}, s.get("deployments_unhealthy", 0))])
    emit("model_monitor_deployments_blocked",
         "관리자가 LiteLLM 에서 일시중지(pause)한 deployment 수. "
         "healthy/unhealthy 어느 쪽에도 포함되지 않는다.", "gauge",
         [({}, s.get("deployments_blocked", 0))])
    emit("model_monitor_blocked_known",
         "LiteLLM 이 일시중지 상태(model_info.blocked)를 알려주면 1. "
         "0 이면 구버전이거나 config 전용 모델이라 '비활성' 판별 자체가 불가.",
         "gauge",
         [({}, 1 if s.get("blocked_known") else 0)])
    emit("model_monitor_model_groups",
         "LiteLLM 모델 그룹 수.", "gauge",
         [({}, s.get("model_groups", 0))])
    emit("model_monitor_backend_pods_ready_total",
         "모든 LB 뒤 ready Pod 합계(공유 Service 는 1회만 집계).", "gauge",
         [({}, s.get("backend_pods_ready", 0))])
    emit("model_monitor_backend_pods_desired_total",
         "모든 LB 뒤 목표 replica 합계(공유 Service 는 1회만 집계).", "gauge",
         [({}, s.get("backend_pods_desired", 0))])
    emit("model_monitor_backend_pods_known",
         "backend Pod 수를 하나라도 알아냈으면 1.", "gauge",
         [({}, 1 if s.get("backend_pods_known") else 0)])
    emit("model_monitor_backend_gpus_ready_total",
         "모든 LB 뒤 ready Pod 가 점유한 GPU 합계(공유 Service 는 1회만 집계).", "gauge",
         [({}, s.get("gpu_total", 0))])
    emit("model_monitor_backend_gpus_known",
         "GPU 정보를 하나라도 알아냈으면 1(RBAC/기능 미설정이면 0).", "gauge",
         [({}, 1 if s.get("gpu_known") else 0)])
    emit("model_monitor_backend_gpus_ready_by_device",
         "장치 모델(H100/B200 등)별 ready GPU 합계(공유 Service dedup, 이기종 GPU 구분).",
         "gauge",
         [({"device": prod}, n)
          for prod, n in sorted((s.get("gpu_products") or {}).items())])

    # --- deployment(모델) 단위 ---
    def base_labels(d):
        lab = {"model": d.get("model_name") or ""}
        if d.get("namespace"):
            lab["namespace"] = d["namespace"]
        if d.get("service"):
            lab["service"] = d["service"]
        return lab

    # 부하 등급 -> 게이지. unknown 은 값을 만들지 않는다(None -> 샘플 제외).
    _LOAD_GAUGE = {"idle": 0, "ok": 1, "busy": 2, "saturated": 3}

    up_s, ready_s, desired_s, s2z_s, gpu_s, blk_s = [], [], [], [], [], []
    run_s, wait_s, kv_s, tps_s, lstate_s, lpods_s, lfail_s = [], [], [], [], [], [], []
    for d in deps:
        lab = base_labels(d)
        up_lab = dict(lab)
        if d.get("status_source"):
            up_lab["status_source"] = d["status_source"]
        up_s.append((up_lab, _STATUS_GAUGE.get(d.get("status"), -1)))
        pod_lab = dict(lab)
        if d.get("backend_source"):
            pod_lab["backend_source"] = d["backend_source"]
        ready_s.append((pod_lab, d.get("backends_ready")))
        desired_s.append((pod_lab, d.get("backends_desired")))
        # GPU 는 backend_source 라벨 없이 model/namespace/service 로만(장치 총량).
        gpu_s.append((dict(lab), d.get("gpu_ready")))
        s2z_s.append(({"model": d.get("model_name") or ""},
                      1 if d.get("scale_to_zero") else 0))
        # 일시중지는 model_up 에서 -1(미상)로 뭉뚱그려지므로 별도 게이지로 뺀다.
        # 기존 DOWN 알림(model_up == 0)은 -1 이라 애초에 안 걸리니 제외 절이 필요
        # 없다. 굳이 명시하려면 라벨 매칭을 반드시 붙일 것 — model_up 에는
        # status_source 라벨이 더 있어 bare unless 는 라벨셋이 달라 **절대 매칭되지
        # 않는다**(조용한 무효 절):
        #   model_monitor_model_up == 0
        #     unless on(model, namespace, service) model_monitor_model_blocked == 1
        blk_s.append((dict(lab), 1 if d.get("status") == "PAUSED" else 0))
        # --- 지금 부하(백엔드 엔진 게이지) ---
        # 값이 없으면(조회 실패·생략) None -> _dedup_samples 가 버린다. 0 으로
        # 채우면 "한가함"과 "모름"이 알림에서 구분되지 않는다.
        lo = d.get("load") or {}
        run_s.append((dict(lab), lo.get("running")))
        wait_s.append((dict(lab), lo.get("waiting")))
        kv_s.append((dict(lab), lo.get("kv_cache_pct")))
        tps_s.append((dict(lab), lo.get("throughput")))
        lstate_s.append((dict(lab), _LOAD_GAUGE.get(lo.get("state"))))
        lpods_s.append((dict(lab), lo.get("pods_sampled") if lo else None))
        lfail_s.append((dict(lab), lo.get("pods_failed") if lo else None))

    emit("model_monitor_model_up",
         "모델 상태: UP=1, DOWN=0, 미상/idle/일시중지=-1. "
         "일시중지 구분은 model_monitor_model_blocked 를 함께 볼 것.", "gauge",
         _dedup_samples(up_s, _status_reduce))
    emit("model_monitor_model_blocked",
         "관리자가 일시중지(pause)해 트래픽을 안 받으면 1. 장애(DOWN)와 구분용.",
         "gauge",
         # 같은 (model,ns,svc) 에 deployment 가 여러 개면 min = '전부 꺼졌을 때만 1'.
         # 하나라도 살아 있으면 그 조합은 여전히 라우팅되므로 1 로 표시하면 거짓
         # 양성이다. 대시보드의 compositeStatus·그래프 노드와 같은 '완전 차단' 규칙.
         _dedup_samples(blk_s, min))
    emit("model_monitor_model_backend_pods_ready",
         "이 모델 LB 뒤 ready Pod 수. 여러 모델이 같은 Service 를 공유할 수 있어 "
         "단순 합산은 물리 Pod 를 중복 집계한다 — 총합은 *_total 사용.", "gauge",
         _dedup_samples(ready_s, max))
    emit("model_monitor_model_backend_pods_desired",
         "이 모델 LB 뒤 목표 replica 수.", "gauge",
         _dedup_samples(desired_s, max))
    emit("model_monitor_model_backend_gpus_ready",
         "이 모델 LB 뒤 ready Pod 가 점유한 GPU 수. 공유 Service 는 여러 모델에 같은 "
         "물리 GPU 가 잡히므로 단순 합산은 이중 집계 — 총합은 *_total 사용.", "gauge",
         _dedup_samples(gpu_s, max))
    # --- 지금 부하 ---
    # 공유 Service dedup 규칙은 pods/GPU 와 동일: 같은 물리 백엔드를 여러 모델이
    # 가리키면 값이 중복되므로 max(합산 아님)로 접는다.
    emit("model_monitor_model_requests_running",
         "지금 처리 중인 요청 수(백엔드 엔진 게이지, Pod 합). 값이 없으면 "
         "샘플이 없다 — 0 과 '모름'을 구분한다.", "gauge",
         _dedup_samples(run_s, max))
    emit("model_monitor_model_requests_waiting",
         "지금 대기 중인 요청 수(큐). 0 보다 크면 이미 사용자가 기다리는 중.",
         "gauge", _dedup_samples(wait_s, max))
    emit("model_monitor_model_kv_cache_usage_percent",
         "KV 캐시 사용률(%). 백엔드 Pod 중 최댓값 — 하나만 포화돼도 그 모델은 아프다.",
         "gauge", _dedup_samples(kv_s, max))
    emit("model_monitor_model_generation_tokens_per_second",
         "지금 생성 속도(tok/s). vLLM 은 카운터 차분이라 두 번째 사이클부터 나온다.",
         "gauge", _dedup_samples(tps_s, max))
    emit("model_monitor_model_load_state",
         "부하 등급: idle=0, ok=1, busy=2(대기 발생/KV 높음), saturated=3(큐 적체/KV 포화). "
         "게이지를 못 읽으면 샘플 자체가 없다(모름 != 한가함).", "gauge",
         _dedup_samples(lstate_s, max))
    emit("model_monitor_model_load_pods_sampled",
         "부하를 실제로 읽어온 Pod 수(표본). pods_failed 와 함께 보면 과소 집계를 알 수 있다.",
         "gauge", _dedup_samples(lpods_s, max))
    emit("model_monitor_model_load_pods_failed",
         "부하 조회에 실패한 Pod 수. 0 보다 크면 위 수치는 일부 Pod 만의 합이다.",
         "gauge", _dedup_samples(lfail_s, max))
    emit("model_monitor_model_scale_to_zero",
         "scale-to-zero 로 0 Pod 가 정상 idle 이면 1(장애 0 Pod 와 구분).", "gauge",
         _dedup_samples(s2z_s, max))

    return "\n".join(lines) + "\n"
