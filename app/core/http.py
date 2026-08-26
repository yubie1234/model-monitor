"""표준 라이브러리(urllib)만 쓰는 HTTP GET 헬퍼.

외부 패키지(requests/httpx)를 끌어들이지 않는 이유: LiteLLM·k8s API 조회는
단순 GET 한 번이면 충분하고, 수집 로직을 air-gapped 친화적인 stdlib 로 유지하기
위해서다. FastAPI 전환 후에도 수집기는 동기(blocking)로 두고, 백그라운드
리프레셔가 asyncio.to_thread 로 이벤트 루프 밖에서 돌린다.
"""

import json
import urllib.error
import urllib.request


def http_get_text(url, api_key=None, timeout=10, accept="text/plain"):
    """GET url -> (ok: bool, text: str|None, error: str|None). 본문을 그대로 반환.

    Prometheus text exposition(백엔드 엔진의 /metrics)처럼 JSON 이 아닌 본문을
    읽을 때 쓴다. 에러 문자열 형식은 http_get_json 과 동일하게 맞춘다
    (호출측이 'timed out' / 'connection error' 를 구분해서 표시하기 때문).
    """
    headers = {"Accept": accept}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return False, None, "HTTP %s %s %s" % (e.code, e.reason, body[:200])
    except urllib.error.URLError as e:
        return False, None, "connection error: %s" % e.reason
    except Exception as e:  # noqa: BLE001
        return False, None, "%s: %s" % (type(e).__name__, e)


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
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        # 에러 본문이 JSON 이면 파싱해서 data 로 함께 돌려준다(ok=False 는 유지;
        # 호출측은 ok 를 먼저 보므로 기존 동작과 호환). LiteLLM /health 는 대상이
        # unhealthy 면 HTTP 503 에 정상 health payload 를 실어 보내는데, 본문을
        # 버리면 유효한 상태 정보를 통째로 잃는다.
        parsed = None
        try:
            parsed = json.loads(body)
        except ValueError:
            pass
        return False, parsed, "HTTP %s %s %s" % (e.code, e.reason, body[:200])
    except urllib.error.URLError as e:
        return False, None, "connection error: %s" % e.reason
    except Exception as e:  # noqa: BLE001
        return False, None, "%s: %s" % (type(e).__name__, e)
