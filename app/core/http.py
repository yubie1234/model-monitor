"""표준 라이브러리(urllib)만 쓰는 HTTP GET 헬퍼.

외부 패키지(requests/httpx)를 끌어들이지 않는 이유: LiteLLM·k8s API 조회는
단순 GET 한 번이면 충분하고, 수집 로직을 air-gapped 친화적인 stdlib 로 유지하기
위해서다. FastAPI 전환 후에도 수집기는 동기(blocking)로 두고, 백그라운드
리프레셔가 asyncio.to_thread 로 이벤트 루프 밖에서 돌린다.
"""

import json
import urllib.error
import urllib.request


def http_get_json(url, api_key=None, timeout=10):
    """GET url -> (ok: bool, data: dict|list|None, error: str|None)."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
        headers["x-api-key"] = api_key  # LiteLLM accepts either
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return True, json.loads(raw), None
            except ValueError:
                return False, None, "non-JSON response: %s" % raw[:200]
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, None, "HTTP %s %s %s" % (e.code, e.reason, body)
    except urllib.error.URLError as e:
        return False, None, "connection error: %s" % e.reason
    except Exception as e:  # noqa: BLE001
        return False, None, "%s: %s" % (type(e).__name__, e)
