"""JSON / Prometheus API 라우트.

요청 경로에서 수집하지 않는다 — 백그라운드 리프레셔가 채운 마지막 스냅샷을
즉시 돌려준다. per-user(키 필수) 모드가 켜지면 무인증 global 경로를 잠그고,
데이터는 키로만(POST /api/snapshot/user) 나간다(admin 키는 전체 뷰 해제).
"""

import asyncio
import hashlib
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.auth import admin_ok, is_admin_key, metrics_ok, request_key
from app.schemas.snapshot import Snapshot
from app.services.prometheus import render_prometheus_metrics
from app.services.user_access import collect_user_access, filter_snapshot_for_user

router = APIRouter(tags=["api"])


@router.get("/api/snapshot", response_model=Snapshot,
            summary="현재 캐시된 스냅샷(라이브 JSON)")
async def api_snapshot(request: Request):
    # 키 필수 모드: 무인증 global 데이터 경로를 잠근다(키로만 조회).
    if request.app.state.user_view_on:
        return JSONResponse(
            {"error": "키 필수 모드입니다 — POST /api/snapshot/user 로 조회하세요.",
             "needs_key": True}, status_code=403)
    return await request.app.state.store.get()


@router.post("/api/snapshot/user", summary="키별(per-user) 필터 스냅샷")
async def api_snapshot_user(request: Request):
    """키별 per-user 뷰. 키는 헤더(X-LiteLLM-Key) 전용(쿼리 금지), 저장·로그 없이
    pass-through. fail-closed: 키 무효면 global 폴백 금지."""
    st = request.app.state
    # 게이트: 운영자가 명시적으로 켜지 않으면 노출 안 함(기본 OFF).
    if not st.user_view_on:
        return JSONResponse(
            {"error": "per-user 뷰가 비활성입니다 (--enable-user-view).",
             "user_view": True}, status_code=403)
    key = request_key(request)
    if not key:
        return JSONResponse(
            {"error": "X-LiteLLM-Key 헤더가 필요합니다.", "user_view": True},
            status_code=400)
    snap = await st.store.get()
    # admin 키(= 모니터 구동 키)면 전체 global 뷰를 비-redacted 로 돌려준다.
    if is_admin_key(st.admin_key, key):
        return JSONResponse(dict(snap, admin_view=True))
    if not st.litellm_url:
        return JSONResponse(
            {"error": "LiteLLM 이 설정되지 않았습니다.", "user_view": True},
            status_code=503)
    # 일반 키: 접근 목록을 짧은 TTL 캐시로 조회(폴링 중복 호출 제거).
    # collect_user_access 는 동기(blocking urllib) LiteLLM 호출이므로 절대 이벤트
    # 루프에서 직접 부르지 않는다 — to_thread 로 워커 스레드에서 돌린다(단일 워커
    # 루프가 LiteLLM 왕복 동안 멈춰 다른 요청·프로브가 타임아웃→ingress 502 나는 것 방지).
    access = await asyncio.to_thread(
        st.access_cache.get_or_collect,
        key,
        lambda: collect_user_access(st.litellm_url, key, st.collect_timeout),
        time.monotonic())
    if not access["ok"]:
        # fail-closed: 절대 unfiltered global 로 폴백하지 않는다.
        return JSONResponse(
            {"error": "유효하지 않거나 만료된 키이거나 LiteLLM 조회에 실패했습니다.",
             "user_view": True}, status_code=401)
    # backend_ref 솔트: 서버 비밀(admin_key)+사용자 키 유래 — 사용자마다 달라
    # '내 뷰 JSON' 간 크로스 상관을 막고, 결정적이라 워커/재기동 간 안정적이다.
    ref_seed = hashlib.sha256(
        ("ref:%s:%s" % (st.admin_key, key)).encode("utf-8")).hexdigest()
    return JSONResponse(
        filter_snapshot_for_user(snap, access, hide_internal=st.hide_internal,
                                 ref_seed=ref_seed))


@router.get("/snapshot.json", include_in_schema=False)
async def snapshot_download(request: Request) -> Response:
    """브라우저에서 클릭 한 번에 파일로 받게 attachment 로 내려준다."""
    # 키 필수 모드면 admin 키 헤더가 있어야 export 허용(전체 데이터 보호).
    if request.app.state.user_view_on and not admin_ok(request):
        return JSONResponse({"error": "export 는 admin 키가 필요합니다."},
                            status_code=403)
    snap = await request.app.state.store.get()
    body = json.dumps(snap, ensure_ascii=False)
    return Response(
        content=body, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="model-monitor-snapshot.json"'})


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """Prometheus 스크레이프. 캐시 스냅샷을 포맷만(수집 안 함)."""
    st = request.app.state
    if not st.metrics_on:
        return PlainTextResponse("not found", status_code=404)
    # 키 필수 모드면 admin 키 헤더 또는 metrics 전용 Bearer 토큰이 있어야 노출
    # (MONITOR_METRICS_TOKEN — Prometheus authorization/PodMonitor secretKeyRef 용).
    if st.user_view_on and not metrics_ok(request):
        return PlainTextResponse(
            "metrics 는 admin 키(X-LiteLLM-Key) 또는 metrics 토큰"
            "(Authorization: Bearer)이 필요합니다.", status_code=403)
    snap = await st.store.get()
    return PlainTextResponse(
        render_prometheus_metrics(snap),
        media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/healthz", include_in_schema=False)
@router.get("/readyz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})
