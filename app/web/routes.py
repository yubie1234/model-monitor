"""웹 대시보드 라우트: 라이브 페이지(/) 와 정지 페이지(/snapshot.html).

수집은 요청 경로에서 하지 않는다. 라이브 페이지는 /api/snapshot 을 폴링하고,
정지 페이지는 현재 스냅샷을 HTML 에 박제(self-contained)해 내려준다.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.auth import admin_ok

router = APIRouter(tags=["web"])

_TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


def load_dashboard_html(interval: float, user_view: bool = False,
                        base_path: str = "") -> str:
    """대시보드 템플릿에 폴링 주기 + per-user 뷰 + path prefix 를 주입해 반환.

    base_path 는 서비스가 prefix(예: /service/model-monitor) 뒤에 있을 때 대시보드의
    자기 호출(fetch/export 링크)에 붙는 접두사. 루트면 빈 문자열.
    """
    html = _TEMPLATE.read_text(encoding="utf-8")
    return (html
            .replace("__INTERVAL_MS__", str(int(interval * 1000)))
            .replace("__USER_VIEW__", "true" if user_view else "false")
            .replace("__BASE_PATH__", base_path))


def frozen_html(html: str, snap: dict) -> str:
    """현재 스냅샷을 페이지에 박제 -> 폴링 없이 그대로 렌더되는 self-contained HTML.

    '<' 를 이스케이프해 데이터 안의 </script> 등이 HTML 을 깨지 않게 한다.
    라이브 대시보드와 같은 렌더 코드를 쓰므로 stale 될 일이 없다.
    """
    blob = json.dumps(snap, ensure_ascii=False).replace("<", "\\u003c")
    inject = "<script>window.__SNAPSHOT__=%s;</script>\n</head>" % blob
    return html.replace("</head>", inject, 1)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(request.app.state.dashboard_html)


@router.get("/snapshot.html", response_class=HTMLResponse, include_in_schema=False)
@router.get("/export", response_class=HTMLResponse, include_in_schema=False)
async def snapshot_html(request: Request):
    # 키 필수 모드면 admin 키 헤더가 있어야 export 허용(전체 데이터 보호).
    if request.app.state.user_view_on and not admin_ok(request):
        return PlainTextResponse("export 는 admin 키가 필요합니다.", status_code=403)
    snap = await request.app.state.store.get()
    return HTMLResponse(frozen_html(request.app.state.dashboard_html, snap))
