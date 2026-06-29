"""per-user(키 필수) 모드의 admin 키 확인 헬퍼.

admin 키 = 모니터를 구동할 때 쓴 LiteLLM 키. 이 키 헤더(X-LiteLLM-Key)가 맞으면
잠긴 global 데이터(export/metrics/전체 스냅샷)에 접근할 수 있다.
"""

import hmac

from fastapi import Request

KEY_HEADER = "X-LiteLLM-Key"


def is_admin_key(admin_key, key):
    """모니터 구동 admin 키와 동일하면 True (상수시간 비교)."""
    return bool(admin_key) and bool(key) and hmac.compare_digest(key, admin_key)


def request_key(request: Request):
    """요청 헤더에서 키를 꺼낸다(쿼리스트링은 로그에 남아 금지 — 헤더 전용)."""
    return (request.headers.get(KEY_HEADER) or "").strip()


def admin_ok(request: Request):
    """export/잠긴 global 접근 허용 여부 — admin 키 헤더가 맞아야 True."""
    return is_admin_key(getattr(request.app.state, "admin_key", ""),
                        request_key(request))
