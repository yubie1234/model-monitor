"""지금 부하(live load): 이 모델이 **지금** 바쁜지.

이 파일이 답하는 질문은 "요청을 보내면 바로 처리되나, 기다리나" 다. 그 답은
LiteLLM 누적 집계로는 알 수 없고, vLLM/SGLang 이 지금 이 순간 노출하는
게이지에만 있다.

수집 원칙 (backend 개수와 동일):
  - **Pod 마다 직접** 읽는다. api_base(=Service LB)로 /metrics 를 찌르면 뒤에
    있는 Pod 중 하나만 응답해서, 3개 중 1개만 큐가 쌓여도 2/3 확률로 놓친다.
    Pod 주소는 gpu.collect_gpu_for_service 가 이미 받아오는 Pod 목록에서
    나온다 -> **k8s 추가 호출 0회**.
  - 못 읽으면 `?` 다. 0 이 아니다. 실패 사유를 그대로 들고 다닌다.
  - 표본을 숨기지 않는다. 3개 중 1개만 답했으면 pods_sampled/pods_failed 로
    드러낸다(화면에서 "(1/3 Pod)").

엔진별 차이는 _PROM_SPECS 한 곳에서 흡수한다. 규칙 셋:
  (a) 이름의 ':' 를 '_' 로 정규화해 `sglang:x` 와 `sglang_x` 를 모두 받는다.
      SGLang 은 소스에 콜론으로 선언돼 있지만 prometheus_client 버전에 따라
      노출이 언더스코어로 나온다(sgl-project/sglang#12618). 콜론만 매칭하면
      그런 빌드에서 게이지를 통째로 못 읽는다.
  (b) 한 필드의 이름 튜플은 **대안(alias)** 이다. 합치지 않고 먼저 있는 것
      하나만 쓴다 — vLLM V0/V1 이름이 함께 노출되는 버전에서 합치면 2배가 된다.
  (c) tp_rank/pp_rank/moe_ep_rank 는 **같은 스케줄러 상태를 복제 보고**하는
      축이라 그룹 안에서 max 로 접고, dp_rank/engine(진짜 다른 워커)만 합산한다.
      안 접으면 TP=4 에서 실행 요청이 4배로 부풀려진다.
"""

import urllib.parse

from app.core.http import http_get_json, http_get_text

# 같은 뜻인데 엔진·버전마다 이름이 다른 게이지들.
_PROM_SPECS = [
    ("vllm", {
        "running": ("vllm:num_requests_running",),
        "waiting": ("vllm:num_requests_waiting", "vllm:num_requests_waiting_by_reason"),
        "kv_cache": ("vllm:kv_cache_usage_perc",        # V1
                     "vllm:gpu_cache_usage_perc"),      # V0
        "gen_tokens": ("vllm:generation_tokens_total", "vllm:generation_tokens"),
        "throughput": (),                               # tok/s 게이지 없음 -> 카운터 차분
    }),
    ("sglang", {
        "running": ("sglang:num_running_reqs",),
        "waiting": ("sglang:num_queue_reqs",),
        "kv_cache": ("sglang:token_usage", "sglang:full_token_usage",
                     "sglang:kv_cache_usage"),
        "gen_tokens": ("sglang:generation_tokens_total",),
        "throughput": ("sglang:gen_throughput",),       # tok/s 를 직접 준다
    }),
]

# 같은 상태를 복제 보고하는 라벨 축(합치면 안 되는 축).
_RANK_LABELS = ("tp_rank", "pp_rank", "moe_ep_rank")

# 부하 등급 판정 기준 (config 의 load.thresholds 로 덮어쓸 수 있다).
LOAD_THRESHOLDS = {
    "queue_busy": 1,        # 대기가 1건이라도 있으면 이미 밀리는 중
    "queue_saturated": 5,   # 큐가 이만큼 쌓이면 포화
    "kv_busy": 80.0,        # KV 캐시 사용률(%)
    "kv_saturated": 95.0,
}

# 정렬·집계용 등급 순서(클수록 바쁨).
LOAD_RANK = {"unknown": -1, "idle": 0, "ok": 1, "busy": 2, "saturated": 3}


def _num(v):
    """숫자로 해석되면 float, 아니면 None (bool 은 숫자로 보지 않는다)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _norm_metric_name(name):
    """sglang_x 와 sglang:x 를 같은 키로 (엔진 접두사 구분자만 정규화)."""
    return name.replace(":", "_", 1)


def parse_prom_metrics(text):
    """Prometheus 텍스트 -> {정규화된 이름: [(labels, 값), ...]}.

    라벨을 버리지 않는다 — rank 축을 접으려면 라벨이 필요하다.
    """
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        labels = {}
        if "{" in line:
            name, rest = line.split("{", 1)
            label_blob, _, tail = rest.rpartition("}")
            for part in label_blob.split(","):
                k, sep, v = part.partition("=")
                if sep:
                    labels[k.strip()] = v.strip().strip('"')
            fields = tail.split()
        else:
            fields = line.split()
            if len(fields) < 2:
                continue
            name, fields = fields[0], fields[1:]
        if not fields:
            continue
        val = _num(fields[0])
        if val is None:
            continue
        out.setdefault(_norm_metric_name(name.strip()), []).append((labels, val))
    return out


def _series(metrics, names):
    """alias 중 **먼저 존재하는 하나**의 시리즈만 반환(이중 집계 방지)."""
    for n in names:
        hit = metrics.get(_norm_metric_name(n))
        if hit:
            return hit
    return None


def _sum_workers(series):
    """rank 복제는 접고(max), 서로 다른 워커는 더한다."""
    groups = {}
    for labels, val in series:
        key = tuple(sorted((k, v) for k, v in labels.items()
                           if k not in _RANK_LABELS))
        groups[key] = max(groups[key], val) if key in groups else val
    return sum(groups.values())


def live_from_metrics(metrics):
    """{이름: [(labels, 값)]} -> Pod 1개의 부하 dict (모르는 값은 None)."""
    if not metrics:
        return None
    for engine, spec in _PROM_SPECS:
        if not any(_series(metrics, names) for names in spec.values() if names):
            continue
        live = {"engine": engine, "running": None, "waiting": None,
                "kv_cache_pct": None, "gen_tokens": None, "throughput": None}
        for field, names in spec.items():
            series = _series(metrics, names) if names else None
            if not series:
                continue
            if field == "kv_cache":
                # 사용률은 더하는 값이 아니다 -> 가장 붐비는 시리즈 기준.
                # 0~1 비율로 주는 게 보통(vLLM·SGLang 둘 다) -> % 로 환산.
                pct = max(v for _, v in series)
                live["kv_cache_pct"] = round(pct * 100.0 if pct <= 1.0 else pct, 1)
            elif field == "throughput":
                live["throughput"] = round(_sum_workers(series), 1)
            elif field == "gen_tokens":
                live["gen_tokens"] = _sum_workers(series)
            else:
                live[field] = int(_sum_workers(series))
        return live
    return None


def live_from_prom(text):
    """/metrics 본문 -> Pod 1개의 부하 dict."""
    return live_from_metrics(parse_prom_metrics(text))


def throughput_from_counter(cache, key, gen_tokens, now):
    """생성 토큰 카운터를 직전 사이클과 차분해 tok/s 산출. 첫 샘플이면 None.

    카운터가 줄었으면(Pod 재시작) 값을 만들지 않고 기준만 다시 잡는다.
    cache 는 사이클 간 유지되는 dict(리프레셔가 넘긴다) — node_cache 와 같은 방식.
    """
    if gen_tokens is None or cache is None:
        return None
    prev = cache.get(key)
    cache[key] = (now, gen_tokens)
    if not prev:
        return None
    dt, dv = now - prev[0], gen_tokens - prev[1]
    if dt <= 0 or dv < 0:
        return None
    return round(dv / dt, 1)


def probe_pod_load(url, timeout, now, tput_cache=None, api_key=None):
    """Pod(또는 LB) 한 곳의 /metrics 조회 -> 부하 dict."""
    ok, text, err = http_get_text(url + "/metrics", api_key, timeout)
    if not ok:
        return {"url": url, "error": err}
    live = live_from_prom(text)
    if live is None:
        return {"url": url,
                "error": "엔진 게이지 없음 — /metrics 는 응답했지만 vllm/sglang "
                         "메트릭이 없음 (SGLang 은 --enable-metrics 필요, "
                         "vLLM 은 --disable-log-stats 확인)"}
    live["url"] = url
    if live.get("throughput") is None:
        live["throughput"] = throughput_from_counter(
            tput_cache, url, live.get("gen_tokens"), now)
    return live


def aggregate_pod_loads(samples, scope):
    """Pod 별 샘플 -> deployment 1행의 부하 요약.

    running/waiting 은 합(그 모델이 지금 물고 있는 전체), KV 는 최댓값(한 Pod 만
    포화돼도 그 모델은 이미 아프다) + 평균을 함께 남긴다.
    """
    oks = [x for x in samples if not x.get("error")]
    out = {
        "scope": scope,                       # pods | lb-sample | prometheus
        "pods_sampled": len(oks),
        "pods_failed": len(samples) - len(oks),
        "engine": oks[0].get("engine") if oks else None,
        "running": None, "waiting": None,
        "kv_cache_pct": None, "kv_cache_avg_pct": None,
        "throughput": None,
        "per_pod": samples,
    }
    if not oks:
        errs = [x.get("error") for x in samples if x.get("error")]
        out["error"] = errs[0] if errs else "샘플 없음"
        return out
    for field in ("running", "waiting"):
        vals = [x[field] for x in oks if x.get(field) is not None]
        if vals:
            out[field] = sum(vals)
    kvs = [x["kv_cache_pct"] for x in oks if x.get("kv_cache_pct") is not None]
    if kvs:
        out["kv_cache_pct"] = round(max(kvs), 1)
        out["kv_cache_avg_pct"] = round(sum(kvs) / len(kvs), 1)
    tps = [x["throughput"] for x in oks if x.get("throughput") is not None]
    if tps:
        out["throughput"] = round(sum(tps), 1)
    return out


# per-user 뷰용 사유 코드 — 원문 에러 문자열 대신 이 고정 집합만 내보낸다
# (원문에는 조회 대상 주소 같은 내부 정보가 섞일 수 있다).
LOAD_REASON_TEXT = {
    "timeout": "응답 없음(타임아웃)",
    "refused": "연결 안 됨",
    "forbidden": "권한 없음",
    "no_metrics": "게이지 없음",
    "skipped_wake_risk": "조회 안 함(깨울 위험)",
    "skipped_external": "조회 안 함(외부 백엔드)",
    "no_sample": "표본 없음",
    "error": "수집 실패",
}


def load_reason_code(load):
    """부하 dict -> 정규화된 사유 코드. 정상 등급이면 None."""
    if not load or load.get("state") not in (None, "unknown"):
        return None
    txt = str(load.get("error") or load.get("state_reason") or "").lower()
    if load.get("scope") == "skipped":
        return "skipped_external" if "external" in txt else "skipped_wake_risk"
    if "timed out" in txt or "timeout" in txt:
        return "timeout"
    if "refused" in txt or "connection" in txt:
        return "refused"
    if "403" in txt or "forbidden" in txt or "401" in txt:
        return "forbidden"
    if "게이지" in txt or "metric" in txt:
        return "no_metrics"
    if "샘플 없음" in txt:
        return "no_sample"
    return "error"


def load_of(d):
    """deployment 행에서 부하 dict 를 꺼낸다.

    admin 뷰는 `load` dict 를 그대로 갖고 있고, per-user 뷰는 리댁션 때문에
    평탄한 스칼라(load_state/load_running/...)만 갖는다 — 집계·렌더가 두 형태를
    따로 처리하지 않도록 여기서 같은 모양으로 되돌린다.
    """
    lo = d.get("load")
    if isinstance(lo, dict):
        return lo
    if d.get("load_state") is None:
        return None
    return {"state": d.get("load_state"),
            "state_reason": d.get("load_reason")
                            or LOAD_REASON_TEXT.get(d.get("load_reason_code"), ""),
            "running": d.get("load_running"), "waiting": d.get("load_waiting"),
            "kv_cache_pct": d.get("load_kv_pct"), "kv_cache_avg_pct": None,
            "throughput": None, "scope": d.get("load_scope"),
            "pods_sampled": d.get("load_pods_sampled") or 0,
            "pods_failed": d.get("load_pods_failed") or 0, "per_pod": []}


def classify_load(load, thresholds=None):
    """부하 dict -> (state, reason). "지금 바쁜가"에 한 단어로 답한다.

    saturated: 큐가 쌓였거나 KV 가 거의 찼다 -> 지금 요청하면 기다린다
    busy:      대기가 생기기 시작했거나 KV 가 높다
    ok:        처리 중이지만 여유 있음
    idle:      아무것도 안 하는 중
    unknown:   못 읽음 — **0 이 아니다**
    """
    t = dict(LOAD_THRESHOLDS)
    t.update(thresholds or {})
    if not load or load.get("error"):
        return "unknown", (load or {}).get("error") or "수집 실패"
    running, waiting = load.get("running"), load.get("waiting")
    kv = load.get("kv_cache_pct")
    if running is None and waiting is None and kv is None:
        return "unknown", "게이지 없음"
    if waiting is not None and waiting >= t["queue_saturated"]:
        return "saturated", "대기 %d건" % waiting
    if kv is not None and kv >= t["kv_saturated"]:
        return "saturated", "KV %.0f%%" % kv
    if waiting is not None and waiting >= t["queue_busy"]:
        return "busy", "대기 %d건" % waiting
    if kv is not None and kv >= t["kv_busy"]:
        return "busy", "KV %.0f%%" % kv
    if running:
        return "ok", "처리 중 %d건" % running
    return "idle", "요청 없음"


def load_targets(deployments, strip_openai_suffix, health_safe=None):
    """deployment 목록 -> probe 대상. Pod 주소가 있으면 Pod 별로 조회한다.

    Pod 주소가 없을 때 LB(api_base)로 폴백하는 건 **위험**하다:
      - Serverless/scale-to-zero 백엔드는 LB 요청이 activator 를 거쳐 **모델을
        깨운다**. 이 프로젝트가 전량 /health 를 기본 off 로 둔 바로 그 이유다.
      - external api_base 는 남의 서비스다(예: api.openai.com). vLLM 게이지가
        있을 리도 없는데 5초마다 GET 을 보내게 된다.
    그래서 폴백은 litellm._deployment_health_safe 로 안전 판정된 비-external
    행에서만 한다. 나머지는 조회하지 않고 scope="skipped" 로 **이유와 함께**
    모름 처리한다 — 조용히 빠지지 않는다.

    같은 (namespace, service) 를 가리키는 api_base 가 둘이면 한 번만 조회하고
    나머지는 별칭으로 묶는다(summarize 의 물리 백엔드 dedup 기준과 동일).
    -> (targets, alias) 를 돌려준다. alias[base] = 실제 조회한 base.
    """
    targets, alias, by_svc = {}, {}, {}
    for d in deployments or []:
        if not d.get("api_base"):
            continue
        base = strip_openai_suffix(d["api_base"])
        if base in targets or base in alias:
            continue
        svc_key = (d.get("namespace"), d.get("service"))
        if svc_key != (None, None) and svc_key in by_svc:
            alias[base] = by_svc[svc_key]      # 같은 Service — 재조회 안 함
            continue
        pods = d.get("backend_pods") or []
        urls = ["http://%s:%s" % (p["ip"], p["port"]) for p in pods
                if p.get("ip") and p.get("port")]
        meta = {"namespace": d.get("namespace"), "service": d.get("service"),
                "pod_names": [p["pod"] for p in pods if p.get("pod")]}
        if urls:
            targets[base] = dict(meta, urls=urls, scope="pods")
        elif d.get("network_type") == "external" or d.get("backend_source") == "external":
            targets[base] = dict(meta, urls=[], scope="skipped",
                                 skip_reason="external 백엔드 — 우리가 직접 조회하지 않음")
        elif health_safe is not None and not health_safe(d):
            targets[base] = dict(
                meta, urls=[], scope="skipped",
                skip_reason="Pod 주소 미확인 + 깨울 위험(serverless/scale-to-zero) "
                            "— LB 조회 생략")
        else:
            # 비 KServe 일반 Service 등 안전 판정된 행만 LB 로 1회 샘플링하고,
            # 표본이 Pod 하나뿐이라는 사실을 scope 로 드러낸다.
            targets[base] = dict(meta, urls=[base], scope="lb-sample")
        if svc_key != (None, None):
            by_svc[svc_key] = base
    return targets, alias


def skipped_load(target, reason=None):
    """조회하지 않은 대상의 부하 값 — 0 이 아니라 '모름'이고, 이유를 들고 다닌다."""
    return {"scope": target.get("scope", "skipped"), "pods_sampled": 0,
            "pods_failed": 0, "engine": None, "running": None, "waiting": None,
            "kv_cache_pct": None, "kv_cache_avg_pct": None, "throughput": None,
            "per_pod": [], "error": reason or target.get("skip_reason") or "조회 생략"}


def aggregate_targets(targets, samples_by_base, thresholds=None):
    """{base: [샘플]} -> {base: 부하 요약(+등급)}. 조회 생략 대상도 포함한다."""
    out = {}
    for base, spec in targets.items():
        if spec.get("scope") == "skipped":
            agg = skipped_load(spec)
        else:
            samples = sorted(samples_by_base.get(base) or [],
                             key=lambda x: x.get("url") or "")
            agg = aggregate_pod_loads(samples, spec["scope"])
        agg["state"], agg["state_reason"] = classify_load(agg, thresholds)
        out[base] = agg
    return out


def collect_load(targets, timeout, now, thresholds=None, tput_cache=None,
                 api_key=None):
    """probe 대상 -> {base: 부하 요약}. **순차** 조회다.

    병렬화는 호출측(Refresher)이 공용 스레드 예산 안에서 한다 — 이 프로젝트는
    수집기가 자체 스레드풀을 만드는 것을 금지한다(app/services/state.py 참고).
    테스트/단발 호출은 이 순차 경로를 그대로 쓰면 된다.
    """
    samples = {}
    for base, spec in targets.items():
        for url in spec.get("urls") or []:
            samples.setdefault(base, []).append(
                probe_pod_load(url, timeout, now, tput_cache, api_key))
    return aggregate_targets(targets, samples, thresholds)


# ---------------------------------------------------------------------------
# Prometheus 폴백: Pod 를 직접 못 긁을 때(NetworkPolicy·mTLS·메트릭 비활성)
#   이미 Prometheus 가 같은 엔진 게이지를 수집 중이면 그걸 대신 읽는다.
#   출처가 같아 정확도는 동일하고, 대신 스크레이프 주기만큼 늦다.
#   ※ app/services/prometheus.py 는 **우리 지표를 내보내는 exporter** 다.
#      여기 있는 건 반대 방향(외부 Prometheus 를 읽는 client).
# ---------------------------------------------------------------------------

def _prom_metric_regex():
    """_PROM_SPECS 가 쓰는 모든 이름(콜론/언더스코어 변형 포함)의 정규식."""
    names = set()
    for _engine, spec in _PROM_SPECS:
        for group in spec.values():
            for n in group:
                names.add(n)
                names.add(_norm_metric_name(n))
    return "|".join(sorted(names))


def build_prom_query(namespace, pods, service, labels=None):
    """PromQL 셀렉터. Pod 이름을 알면 Pod 로, 모르면 Service 로 좁힌다.

    namespace/pod 라벨을 함께 걸어 다른 모델의 시리즈가 섞이지 않게 한다.
    """
    lab = {"namespace": "namespace", "pod": "pod", "service": "service"}
    lab.update(labels or {})
    parts = ['__name__=~"%s"' % _prom_metric_regex()]
    if namespace:
        parts.append('%s="%s"' % (lab["namespace"], namespace))
    if pods:
        parts.append('%s=~"%s"' % (lab["pod"], "|".join(sorted(pods))))
    elif service:
        parts.append('%s="%s"' % (lab["service"], service))
    return "{%s}" % ",".join(parts)


def prom_instant_query(base, query, timeout, lookback="2m", api_key=None):
    """GET /api/v1/query -> ([(name, labels, value)], error).

    lookback_delta 로 조회 구간을 좁힌다(Prometheus 기본 5분). 이게 없으면
    스크레이프가 멈춘 몇 분 전 값이 '지금 부하'로 둔갑한다.
    """
    url = ("%s/api/v1/query?query=%s&lookback_delta=%s"
           % (base.rstrip("/"), urllib.parse.quote(query), lookback))
    ok, data, err = http_get_json(url, api_key, timeout)
    if not ok:
        return None, err
    if not isinstance(data, dict) or data.get("status") != "success":
        return None, "prometheus: %s" % (
            (data or {}).get("error") or "unexpected response")
    series = []
    for item in ((data.get("data") or {}).get("result")) or []:
        metric = dict(item.get("metric") or {})
        name = metric.pop("__name__", None)
        val = _num((item.get("value") or [None, None])[1])
        if name and val is not None:
            series.append((name, metric, val))
    return series, None


def load_from_prom_series(series, now, tput_cache=None, labels=None):
    """Prometheus 시리즈 -> Pod 별 샘플 목록(직접 긁은 것과 같은 모양)."""
    lab = {"pod": "pod", "instance": "instance"}
    lab.update(labels or {})
    by_pod = {}
    for name, metric, val in series:
        key = metric.get(lab["pod"]) or metric.get(lab["instance"]) or "-"
        by_pod.setdefault(key, {}).setdefault(
            _norm_metric_name(name), []).append((metric, val))
    samples = []
    for pod, metrics in sorted(by_pod.items()):
        live = live_from_metrics(metrics)
        url = "prom:%s" % pod
        if live is None:
            samples.append({"url": url, "error": "엔진 게이지 없음(Prometheus)"})
            continue
        live["url"] = url
        if live.get("throughput") is None:
            live["throughput"] = throughput_from_counter(
                tput_cache, url, live.get("gen_tokens"), now)
        samples.append(live)
    return samples


def collect_load_via_prometheus(targets, settings, now, only=None,
                                tput_cache=None):
    """Prometheus 에서 부하를 받아온다. only 를 주면 그 base 들만(폴백 모드)."""
    base_url = settings.get("prometheus_url")
    if not base_url:
        return {}
    timeout = float(settings.get("prometheus_timeout") or settings.get("timeout") or 10)
    lookback = settings.get("prometheus_lookback") or "2m"
    labels = settings.get("prometheus_labels") or {}
    empty = {"scope": "prometheus", "pods_sampled": 0, "pods_failed": 0,
             "engine": None, "running": None, "waiting": None,
             "kv_cache_pct": None, "kv_cache_avg_pct": None,
             "throughput": None, "per_pod": []}
    out = {}
    for base, spec in targets.items():
        if only is not None and base not in only:
            continue
        if spec.get("scope") == "skipped":
            continue      # 조회하지 않기로 한 대상은 Prometheus 로도 되살리지 않는다
        query = build_prom_query(spec.get("namespace"), spec.get("pod_names"),
                                 spec.get("service"), labels)
        series, err = prom_instant_query(base_url, query, timeout, lookback,
                                         settings.get("prometheus_api_key"))
        if series is None:
            out[base] = dict(empty, pods_failed=1,
                             error="prometheus: %s" % err)
        elif not series:
            out[base] = dict(
                empty,
                error="prometheus: 최근 %s 안에 샘플 없음(스크레이프 중단?)" % lookback)
        else:
            out[base] = aggregate_pod_loads(
                load_from_prom_series(series, now, tput_cache, labels),
                "prometheus")
        out[base]["state"], out[base]["state_reason"] = classify_load(
            out[base], settings.get("load_thresholds"))
    return out


def merge_load_sources(direct, prom):
    """직접 조회 결과에 Prometheus 결과를 **표본이 더 많을 때만** 덮어쓴다.

    폴백이 원래 값을 더 나쁘게 만들면 안 된다: Pod 3개를 직접 읽었는데
    Prometheus 가 1개만 알고 있으면 직접 읽은 쪽이 낫다.
    """
    out = dict(direct)
    for base, pl in (prom or {}).items():
        cur = out.get(base) or {}
        if (pl.get("pods_sampled") or 0) > (cur.get("pods_sampled") or 0):
            out[base] = pl
    return out


def attach_load_to_deployments(deployments, load_by_base, strip_openai_suffix,
                               alias=None):
    """base URL 기준으로 부하 요약을 deployment 행에 붙인다.

    alias 는 같은 (ns,svc) 라 조회를 공유한 base 들의 매핑이다 — 같은 물리
    백엔드를 가리키는 행은 같은 값을 받는다.
    """
    out = []
    for d in deployments or []:
        base = strip_openai_suffix(d["api_base"]) if d.get("api_base") else None
        if base and alias:
            base = alias.get(base, base)
        load = load_by_base.get(base) if base else None
        out.append(dict(d, load=load) if load else d)
    return out
