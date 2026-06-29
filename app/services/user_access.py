"""per-user(키별) 뷰: 키 접근 수집 + 짧은 캐시 + global 스냅샷 필터.

핵심: 무거운 데이터(상태·Pod 수·model/info·/health)는 admin 키로 백그라운드에서
한 번만 수집해 공유 캐시에 둔다(키 무관). 요청이 오면 이 캐시를 "그 키가 접근
가능한 model_name 집합" 으로 **필터**만 한다(얇은 레이어).
"""

import copy
import hashlib
import threading

from app.core.http import http_get_json
from app.services.snapshot import summarize


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
    **성공(ok=True) 응답만** 캐시한다 — 무효/만료 키는 매 요청 재검증되어 fail-closed
    즉시성이 유지된다. 대신 취소/만료된 *유효했던* 키는 최대 TTL 동안 stale 할 수 있다.
    """

    def __init__(self, ttl=30.0, maxsize=512):
        self.ttl = ttl
        self.maxsize = maxsize
        self._d = {}            # sha256(key) -> (expiry, access)
        self._lock = threading.Lock()

    def get_or_collect(self, key, collect, now):
        """캐시에 살아있으면 그대로, 아니면 collect() 호출 후(성공 시) 캐시."""
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            ent = self._d.get(h)
            if ent and ent[0] > now:
                return ent[1]
        access = collect()
        if isinstance(access, dict) and access.get("ok"):
            with self._lock:
                self._prune(now)
                self._d[h] = (now + self.ttl, access)
        return access

    def _prune(self, now):
        if len(self._d) < self.maxsize:
            return
        for k in [k for k, (e, _) in self._d.items() if e <= now]:
            self._d.pop(k, None)
        while len(self._d) >= self.maxsize:
            self._d.pop(next(iter(self._d)), None)


def _redact_deployment_for_user(d):
    """per-user 뷰에서 내부 토폴로지(api_base/underlying/namespace/내부 URL)를 떼고
    상태·종류·backend Pod 수만 남긴다(비-admin 에 클러스터 구조 비노출)."""
    return {
        "model_name": d.get("model_name"),
        "type": d.get("type", "-"),
        "status": d.get("status", "?"),
        "status_source": d.get("status_source"),
        "backends_ready": d.get("backends_ready"),
        "backends_desired": d.get("backends_desired"),
        "backend_source": d.get("backend_source"),
        "scale_to_zero": d.get("scale_to_zero"),
        "mode": d.get("mode"),
    }


def filter_snapshot_for_user(global_snap, access, hide_internal=True):
    """global 스냅샷을 사용자가 접근 가능한 모델로 필터한 per-user 뷰를 만든다.

    핵심: 상태·Pod 수는 **deployment 단위라 키와 무관** → global 값을 그대로 join 하고,
    "이 키가 접근 가능한 model_name 집합" 으로 걸러내기만 한다(얇은 레이어).

    ⚠️ **공유 캐시 오염 주의** — 서버는 단일 스냅샷을 공유한다(얕은 복사).
    반드시 **deepcopy 한 사본 위에서** 필터할 것. global 의 deployments/groups 를
    제자리(in-place) 로 필터하면 모든 사용자의 global 뷰가 깨진다.
    """
    accessible = set(access.get("accessible") or [])
    snap = copy.deepcopy(global_snap)
    snap["user_view"] = True
    snap.pop("loading", None)
    if hide_internal:
        # 백그라운드 수집 에러 문자열에 내부 주소가 섞일 수 있어 비-admin 뷰에선 숨긴다.
        snap.pop("collect_error", None)
    ll = snap.get("litellm")
    if ll:
        deps = [d for d in (ll.get("deployments") or [])
                if d.get("model_name") in accessible]
        ll["deployments"] = ([_redact_deployment_for_user(d) for d in deps]
                             if hide_internal else deps)
        ll["groups"] = [g for g in (ll.get("groups") or [])
                        if g.get("model_group") in accessible]
        # /v1/models 목록은 사용자 키 기준으로 교체(global admin 목록 노출 금지).
        ll["models"] = sorted(accessible)
        if hide_internal:
            # 수집 에러 문자열에 내부 api_base 가 섞일 수 있어 비-admin 뷰에선 숨긴다.
            ll["errors"] = []
            ll.pop("health", None)
            ll.pop("url", None)
    # 필터된 deployments 기준으로 summary 재계산(카드 수치가 표와 일치).
    snap["summary"] = summarize(snap)
    snap["key_info"] = access.get("key_info")
    snap["accessible_count"] = len(accessible)
    return snap
