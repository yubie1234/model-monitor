"""per-user(키별) 뷰: 키 접근 수집 + 짧은 캐시 + global 스냅샷 필터.

핵심: 무거운 데이터(상태·Pod 수·model/info·/health)는 admin 키로 백그라운드에서
한 번만 수집해 공유 캐시에 둔다(키 무관). 요청이 오면 이 캐시를 "그 키가 접근
가능한 model_name 집합" 으로 **필터**만 한다(얇은 레이어).
"""

import copy
import hashlib
import secrets
import threading

from app.core.http import http_get_json
from app.services.snapshot import summarize
from app.services.load import load_reason_code


def collect_user_access(url, user_key, timeout):
    """사용자 본인 키로 접근 가능한 모델 집합 + 키 메타를 수집(per-user 뷰용).

    권한 판정의 **단일 출처는 `/v1/models`** — LiteLLM 이 키의 models/팀 상속/
    `*`·`openai/*` 와일드카드/access group 을 **이미 해석한 결과**다. 우리가 이를
    재유도하지 않는다(`/key/info.models` 로 접근권을 다시 풀면 피하려던 해석이 되살아남).

    `/key/info` 는 메타(spend/budget/limit) 표시용으로만 쓴다 — best-effort 라
    실패해도 모델 목록(접근권)에는 영향 없다.

    fail-closed: `/v1/models` 가 실패(키 무효/만료/네트워크)하면 ok=False, accessible
    은 빈 집합으로 둔다. 호출측은 절대 unfiltered global 로 폴백하면 안 된다.
    """
    base = url.rstrip("/")
    out = {"ok": False, "error": None, "accessible": [], "key_info": None}

    ok, data, err = http_get_json(base + "/v1/models", user_key, timeout)
    if not ok:
        out["error"] = err or "/v1/models 조회 실패"
        return out
    if not isinstance(data, dict):
        out["error"] = "예상치 못한 /v1/models 응답"
        return out
    # /v1/models 의 id == /model/info 의 model_name(public name) 으로 조인한다.
    out["accessible"] = sorted(
        {m.get("id") for m in (data.get("data") or []) if m.get("id")})
    out["ok"] = True

    # 키 메타: 비-admin 키가 자기 키 정보를 못 읽는 버전도 있어 best-effort.
    ok2, ki, _ = http_get_json(base + "/key/info", user_key, timeout)
    if ok2 and isinstance(ki, dict):
        info = ki.get("info") if isinstance(ki.get("info"), dict) else ki
        out["key_info"] = {
            "spend": info.get("spend"),
            "max_budget": info.get("max_budget"),
            "tpm_limit": info.get("tpm_limit"),
            "rpm_limit": info.get("rpm_limit"),
            "expires": info.get("expires"),
            "key_alias": info.get("key_alias"),
        }
    return out


class AccessCache:
    """키별 접근 결과(`collect_user_access`)를 짧게 캐시 — 폴링 중복 호출 제거.

    캐시 키는 **원문 키가 아니라 sha256 해시**(키는 절대 저장 안 함).

    성공(ok=True)은 `ttl`(길게, 기본 30s), 실패(무효/만료/네트워크)는 `fail_ttl`
    (짧게, 기본 3s)로 **둘 다 캐시**한다. 실패도 캐시하는 이유: 대시보드가 5초마다
    폴링하는데 실패를 캐시 안 하면 무효 키가 매 폴링마다 blocking LiteLLM 왕복
    (/v1/models + /key/info, 각 timeout)을 새로 일으켜 스레드·CPU 를 잡아먹고
    ingress 502 를 유발한다. fail_ttl 을 짧게 둬 fail-closed 즉시성은 유지한다
    (무효→유효로 바뀐 키는 최대 fail_ttl 뒤 재검증). 네거티브 캐시는 접근을
    **부여**하지 않고 실패 결과(빈 accessible)만 잠깐 재사용하므로 보안상 안전하다.
    """

    def __init__(self, ttl=30.0, maxsize=512, fail_ttl=3.0):
        self.ttl = ttl
        self.fail_ttl = fail_ttl
        self.maxsize = maxsize
        self._d = {}            # sha256(key) -> (expiry, access)
        self._lock = threading.Lock()

    def get(self, key, now):
        """살아있는 캐시 항목만 반환(수집 없음) — 요청 경로의 세마포어 선회피용.

        락 잡힌 dict 룩업 1회라 이벤트 루프에서 직접 불러도 안전하다. 미스/만료는
        None — 호출측이 세마포어를 잡고 get_or_collect 로 넘어간다."""
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            ent = self._d.get(h)
            if ent and ent[0] > now:
                return ent[1]
        return None

    def get_or_collect(self, key, collect, now):
        """캐시에 살아있으면 그대로, 아니면 collect() 후 결과별 TTL 로 캐시."""
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            ent = self._d.get(h)
            if ent and ent[0] > now:
                return ent[1]
        access = collect()
        if isinstance(access, dict):
            ttl = self.ttl if access.get("ok") else self.fail_ttl
            if ttl > 0:
                with self._lock:
                    self._prune(now)
                    self._d[h] = (now + ttl, access)
        return access

    def _prune(self, now):
        if len(self._d) < self.maxsize:
            return
        for k in [k for k, (e, _) in self._d.items() if e <= now]:
            self._d.pop(k, None)
        while len(self._d) >= self.maxsize:
            self._d.pop(next(iter(self._d)), None)


# 익명 backend 식별자용 프로세스 솔트(ref_seed 미제공 시 폴백). 솔트 없이 (ns,svc)를
# 해시하면 흔한 서비스 이름 사전대입으로 역산될 수 있어 비-admin 노출에 부적합하다.
_REF_SALT = secrets.token_hex(8)


def _backend_ref(d, seed=None):
    """익명 백엔드 식별자(8자 hex). 식별 근거가 전혀 없으면 None.

    per-user 뷰는 Service/api_base 를 숨기지만, '어떤 deployment 들이 같은
    백엔드를 공유하는가' 토폴로지는 이 값으로 유지된다 — 이름 노출 없이
    Model↔Backend 그래프와 공유(⇄) 표시를 그릴 수 있게 한다.

    기반: (ns,svc) → api_base → id → model_name 순 폴백. api_base 도 없는
    deployment(openai 등)가 여러 개면 서로 다른 ref 를 받아야 한다 — 하나의
    'external' 키로 뭉치면 그래프에 거짓 공유(⇄)가 생긴다.

    seed(권장: 서버 비밀+사용자 키 유래, 호출측이 전달)를 주면 사용자마다 다른
    ref 가 나와 두 사용자가 '내 뷰 JSON' 을 대조해 백엔드 공유 관계를 상관
    분석하는 것을 막고, 값이 결정적이라 워커/재기동이 달라도 ref 가 안정적이다.
    미제공 시 프로세스 솔트 폴백(프로세스 내 일관, 재기동 시 변경 — 표시 전용).
    """
    basis = "%s/%s" % (d.get("namespace") or "", d.get("service") or "")
    if basis == "/":
        basis = d.get("api_base") or d.get("id") or d.get("model_name") or ""
    if not basis:
        return None
    salt = seed if seed else _REF_SALT
    return hashlib.sha256((salt + basis).encode("utf-8")).hexdigest()[:8]


# per-user 뷰의 부하 노출 단계. 정식 값은 config.normalize_user_load 가 만든다 —
# 여기서는 모르는 값이 흘러와도 **더 적게 보여주는 쪽**(summary)으로 떨어뜨린다.
USER_LOAD_MODES = ("off", "summary", "detail")


def _redact_deployment_for_user(d, ref_seed=None, load_mode="detail"):
    """per-user 뷰에서 내부 토폴로지(api_base/underlying/namespace/내부 URL)를 떼고
    상태·종류·backend Pod 수만 남긴다(비-admin 에 클러스터 구조 비노출).
    백엔드 식별은 익명 backend_ref 로만 제공한다."""
    out = {
        "model_name": d.get("model_name"),
        "type": d.get("type", "-"),
        "network_type": d.get("network_type", "-"),
        "backend_type": d.get("backend_type", "-"),
        "status": d.get("status", "?"),
        "status_source": d.get("status_source"),
        "backends_ready": d.get("backends_ready"),
        "backends_desired": d.get("backends_desired"),
        "backend_source": d.get("backend_source"),
        "scale_to_zero": d.get("scale_to_zero"),
        "mode": d.get("mode"),
        "backend_ref": _backend_ref(d, ref_seed),
    }
    # 일시중지는 내부 토폴로지가 아니라 "내 모델이 왜 응답이 없나" 의 답이라
    # 비-admin 뷰에도 남긴다(health_status 도 상태 문자열일 뿐).
    # 단 **있을 때만** 넣는다: 위 dict 처럼 무조건 넣으면 값이 None 이어도 키가
    # 생겨 summarize 의 blocked_known(= 키 존재 여부)이 항상 참이 되고,
    # blocked 를 모르는 LiteLLM 에서도 "판별 가능" 이라고 거짓 보고하게 된다.
    for k in ("blocked", "health_status", "health_status_source"):
        if k in d:
            out[k] = d[k]
    # 지금 부하도 내부 토폴로지가 아니라 "지금 이 모델을 쓸 수 있나"의 답이라 남긴다.
    # 단 **평탄한 스칼라로만** — load dict 안의 per_pod 에는 조회한 Pod 주소가 들어
    # 있어 그대로 넘기면 클러스터 내부가 새어 나간다. 사유도 원문 대신 정규화 코드로.
    #
    # 어디까지 보여줄지는 운영자가 정한다(load_mode):
    #   off     — 키 자체를 만들지 않는다. 대시보드에서도 컬럼이 사라진다.
    #   summary — 등급 + 사유 코드 + 부분표본 여부만. 사용자의 질문에는 답하되
    #             처리중/대기/KV/표본 수 같은 운영 수치는 내보내지 않는다.
    #   detail  — 아래 수치까지(그래도 per_pod/Pod 주소는 절대 나가지 않는다).
    lo = d.get("load")
    if load_mode != "off" and isinstance(lo, dict) and lo.get("state"):
        out["load_state"] = lo["state"]
        code = load_reason_code(lo)
        if code:
            out["load_reason_code"] = code
        elif lo.get("state_reason") and load_mode == "detail":
            # 정상 등급의 근거("대기 9건")는 숫자와 상태뿐이라 detail 에서는 그대로.
            # summary 에서는 이것도 수치라 뺀다 — 등급만 남기는 것이 이 모드의 뜻이다.
            out["load_reason"] = lo["state_reason"]
        if load_mode == "detail":
            if lo.get("scope"):
                out["load_scope"] = lo["scope"]
            # 값이 없는 항목은 키 자체를 만들지 않는다 — 무조건 None 을 넣으면
            # "측정했는데 0" 과 "모름" 이 구분되지 않는다(blocked 와 같은 이유).
            for src, dst in (("running", "load_running"),
                             ("waiting", "load_waiting"),
                             ("kv_cache_pct", "load_kv_pct"),
                             ("pods_sampled", "load_pods_sampled"),
                             ("pods_failed", "load_pods_failed")):
                if lo.get(src) is not None:
                    out[dst] = lo[src]
        elif lo.get("pods_failed"):
            # summary 라도 "일부 Pod 을 못 읽고 낸 등급" 이라는 사실은 남긴다 —
            # 표본 수(수치) 대신 불리언으로. 불완전한 값을 완전한 척 보여주지 않는다.
            out["load_partial"] = True
    return out


# 스냅샷 최상위의 부하 관련 키. load_mode="off" 면 통째로 빠진다 —
# load_enabled 가 대시보드의 LOAD 컬럼 스위치라 이것만 내려도 화면에서 사라진다.
_LOAD_TOP_KEYS = ("load_enabled", "load_routing", "load_ts_epoch",
                  "load_interval")


def _slim_load(lo):
    """show_internal 뷰용 summary 축약 — 등급만 남기고 수치/Pod 은 버린다."""
    if not isinstance(lo, dict) or not lo.get("state"):
        return None
    out = {"state": lo["state"], "per_pod": []}
    if load_reason_code(lo):
        out["state_reason"] = lo.get("state_reason") or ""
    if lo.get("pods_failed"):
        out["partial"] = True
    return out


def filter_snapshot_for_user(global_snap, access, hide_internal=True,
                             ref_seed=None, load_mode="detail"):
    """global 스냅샷을 사용자가 접근 가능한 모델로 필터한 per-user 뷰를 만든다.

    핵심: 상태·Pod 수는 **deployment 단위라 키와 무관** → global 값을 그대로 join 하고,
    "이 키가 접근 가능한 model_name 집합" 으로 걸러내기만 한다(얇은 레이어).

    ⚠️ **공유 캐시 오염 주의** — 서버는 단일 스냅샷을 공유한다(얕은 복사).
    global 의 deployments/groups 를 제자리(in-place) 로 필터하면 모든 사용자의
    global 뷰가 깨진다. 그래서 **접근 가능한 것만 골라 새 컨테이너로 복사**하고,
    남는 값도 mutable 이면 deepcopy 해서 global 과 객체를 공유하지 않게 한다.

    ⚡ **필터를 복사보다 먼저** 한다. 예전엔 스냅샷을 통째로 deepcopy 한 뒤
    걸러냈는데, 그러면 접근 불가 모델까지 전부 복사한 뒤 버려서 비용이 사용자
    키와 무관하게 **전체 배포 수**에 비례했다(폴링 주기 × 사용자 수만큼 반복).
    실측: 배포 1000개 중 50개 접근 가능한 사용자 13.9ms -> 0.21ms.
    """
    accessible = set(access.get("accessible") or [])
    # 모르는 값이 흘러와도 더 적게 보여주는 쪽으로 (정식화는 config 계층의 몫).
    if load_mode not in USER_LOAD_MODES:
        load_mode = "summary"

    def _cp(v):
        """global 과 객체를 공유하지 않게: 컨테이너만 deepcopy, 스칼라는 그대로."""
        return copy.deepcopy(v) if isinstance(v, (dict, list, set)) else v

    # 최상위는 원본 키 순서를 유지해 재구성한다(litellm 만 아래에서 새로 만든다).
    snap = {}
    for k, v in global_snap.items():
        if k == "loading":
            continue                     # per-user 뷰엔 노출하지 않음
        if k == "collect_error" and hide_internal:
            continue                     # 에러 문자열에 내부 주소가 섞일 수 있다
        if k == "summary":
            snap[k] = None               # 아래에서 필터 결과로 재계산 (자리만 예약)
            continue
        if load_mode == "off" and k in _LOAD_TOP_KEYS:
            continue                     # 부하 비노출: 갱신 시각·주기까지 안 준다
        if k != "litellm":
            snap[k] = _cp(v)
            continue

        ll = v
        if not ll:
            snap["litellm"] = _cp(ll)    # None/빈 dict 는 그대로 (필터할 것이 없다)
            continue
        new_ll = {}
        for lk, lv in ll.items():
            if lk in ("deployments", "groups", "models"):
                continue                 # 아래에서 필터된 값으로 채운다
            if hide_internal and lk in ("health", "url", "errors"):
                continue                 # 내부 주소가 섞일 수 있는 필드
            new_ll[lk] = _cp(lv)
        deps = [d for d in (ll.get("deployments") or [])
                if d.get("model_name") in accessible]
        # 리댁션은 새 dict 를 만들고 값이 전부 스칼라라 별도 복사가 필요 없다.
        # hide_internal=False(관리자 의도)면 원본 행을 쓰므로 deepcopy 가 필수다.
        if hide_internal:
            new_ll["deployments"] = [
                _redact_deployment_for_user(d, ref_seed, load_mode) for d in deps]
        else:
            # show_internal 뷰는 원본 행을 그대로 쓰므로 deepcopy 가 필수다.
            # 부하 단계는 여기서도 지킨다 — "내부까지 보여준다"와 "부하를 어디까지
            # 보여준다"는 별개의 결정이고, 운영자가 off/summary 를 골랐으면 그 뜻이다.
            rows = copy.deepcopy(deps)
            if load_mode != "detail":
                for r in rows:
                    lo = r.pop("load", None)
                    slim = _slim_load(lo) if load_mode == "summary" else None
                    if slim:
                        r["load"] = slim
            new_ll["deployments"] = rows
        new_ll["groups"] = copy.deepcopy(
            [g for g in (ll.get("groups") or [])
             if g.get("model_group") in accessible])
        # /v1/models 목록은 사용자 키 기준으로 교체(global admin 목록 노출 금지).
        new_ll["models"] = sorted(accessible)
        if hide_internal:
            new_ll["errors"] = []
        snap["litellm"] = new_ll

    snap["user_view"] = True
    # 필터된 deployments 기준으로 summary 재계산(카드 수치가 표와 일치).
    snap["summary"] = summarize(snap)
    snap["key_info"] = access.get("key_info")
    snap["accessible_count"] = len(accessible)
    return snap
