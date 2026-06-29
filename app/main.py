"""FastAPI 애플리케이션 진입점.

  uvicorn app.main:app --host 0.0.0.0 --port 8088
  python -m app                       # (간편 실행 래퍼, Settings 의 host/port 사용)

수집은 요청 경로가 아니라 lifespan 이 띄우는 백그라운드 리프레셔가 담당한다.
HTTP 핸들러는 항상 마지막 캐시 스냅샷을 즉시 돌려준다.
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from app import __version__
from app.api.routes import router as api_router
from app.config import (
    Settings,
    build_collector_settings,
    get_settings,
    normalize_root_path,
)
from app.services.state import Refresher, SnapshotStore
from app.services.user_access import AccessCache
from app.web.routes import load_dashboard_html, router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresher: Refresher = app.state.refresher
    await refresher.start()
    try:
        yield
    finally:
        await refresher.stop()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    collector_settings = build_collector_settings(settings)
    # path prefix 뒤 배포 지원(예: /service/model-monitor). 비면 루트.
    root_path = normalize_root_path(settings.root_path)

    app = FastAPI(
        title="model-monitor",
        version=__version__,
        description="LiteLLM → KServe → vLLM/SGLang 모델 현황 + LB 뒤 backend Pod 개수 모니터",
        lifespan=lifespan,
        # root_path: /docs·openapi 링크가 prefix 를 포함하도록(Ingress 가 prefix 를 떼는 전제).
        root_path=root_path,
    )

    store = SnapshotStore()
    app.state.settings = settings
    app.state.collector_settings = collector_settings
    app.state.store = store
    app.state.refresher = Refresher(
        collector_settings, store, settings.interval, demo=settings.demo)

    # --- per-user(키 필수) 뷰 / Prometheus / export 접근 제어용 상태 ---
    # 키 필수 모드는 데모에선 끈다(데모는 라이브 키 검증이 없음).
    app.state.user_view_on = bool(collector_settings["user_view"]) and not settings.demo
    app.state.hide_internal = bool(collector_settings["user_view_hide_internal"])
    app.state.metrics_on = bool(collector_settings["metrics"])
    app.state.admin_key = collector_settings.get("api_key") or ""
    app.state.litellm_url = collector_settings.get("litellm_url")
    app.state.collect_timeout = collector_settings.get("timeout", 10.0)
    app.state.access_cache = AccessCache(
        ttl=float(collector_settings["user_view_cache_ttl"]))

    app.state.root_path = root_path
    app.state.dashboard_html = load_dashboard_html(
        settings.interval, app.state.user_view_on, base_path=root_path)

    app.include_router(api_router)
    app.include_router(web_router)
    return app


app = create_app()
