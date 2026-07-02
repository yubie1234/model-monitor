"""per-user(키 필수) 모드의 admin 키 확인 헬퍼.

admin 키 = 모니터를 구동할 때 쓴 LiteLLM 키. 이 키 헤더(X-LiteLLM-Key)가 맞으면
잠긴 global 데이터(export/metrics/전체 스냅샷)에 접근할 수 있다.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 타입 힌트 전용 — auth 로직 자체는 FastAPI 없이도 동작/테스트된다.
    from fastapi import Request

KEY_HEADER = "X-LiteLLM-Key"


def _ct_eq(a, b):
    """상수시간 비교 — str 그대로 비교하면 non-ASCII 헤더에 TypeError(→500)가
    나므로 bytes 로 인코딩해 비교한다(틀린 키는 조용히 False)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_admin_key(admin_key, key):
    """모니터 구동 admin 키와 동일하면 True (상수시간 비교)."""
    return bool(admin_key) and bool(key) and _ct_eq(key, admin_key)


def request_key(request: Request):
    """요청 헤더에서 키를 꺼낸다(쿼리스트링은 로그에 남아 금지 — 헤더 전용)."""
    return (request.headers.get(KEY_HEADER) or "").strip()


def admin_ok(request: Request):
    """export/잠긴 global 접근 허용 여부 — admin 키 헤더가 맞아야 True."""
    return is_admin_key(getattr(request.app.state, "admin_key", ""),
                        request_key(request))


def bearer_token(request: Request):
    """Authorization: Bearer <token> 값. Prometheus scrape 설정의 `authorization`
    (PodMonitor 는 secretKeyRef)이 이 형태로 보낸다 — 임의 헤더가 안 되는
    스크레이퍼도 표준 방식으로 인증할 수 있다."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def metrics_ok(request: Request):
    """키 필수 모드에서 /metrics 스크레이프 허용 여부.

    admin 키 헤더(X-LiteLLM-Key) **또는** metrics 전용 Bearer 토큰
    (MONITOR_METRICS_TOKEN / metrics.token) 중 하나가 맞으면 True.
    LiteLLM admin 키를 Prometheus 에 배포하지 않고도 스크레이프를 허용하기 위한
    별도 자격이다(메트릭 조회만 가능, 스냅샷/export 는 열리지 않음).
    토큰 미설정이면 Bearer 경로는 fail-closed(admin 키만 유효).
    """
    if admin_ok(request):
        return True
    token = getattr(request.app.state, "metrics_token", "") or ""
    presented = bearer_token(request)
    return bool(token) and bool(presented) and _ct_eq(presented, token)
