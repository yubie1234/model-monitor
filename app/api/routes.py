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
    # 캐시 스냅샷을 JSONResponse 로 바로 내린다 — dict 를 반환하면 폴링 요청마다
    # response_model(Snapshot) Pydantic 재검증·재직렬화 CPU 를 쓴다(Response 반환
    # 시 FastAPI 는 이를 건너뛰고, response_model 은 OpenAPI 문서용으로 남는다).
    return JSONResponse(await request.app.state.store.get())


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
    # 고유 키가 늘어나도(잘못된 키 대입 포함) blocking LiteLLM 왕복이 수집 스레드풀
    # (_COLLECT_THREADS=8)을 독식하지 않게 동시 조회를 세마포어로 캡한다 — 초과
    # 요청은 스레드가 아니라 이벤트 루프에서 가볍게 대기한다.
    # 캐시 히트는 세마포어를 우회한다 — LiteLLM 이 느릴 때 미스 4건이 슬롯을 다
    # 물고 있어도, 이미 검증된 폴링 사용자(warm 캐시)는 대기 없이 즉시 응답한다
    # (선조회는 락 잡힌 dict 룩업 1회 — 마이크로초 단위라 루프 직접 호출 안전).
    access = st.access_cache.get(key, time.monotonic())
    if access is None:
        async with st.user_access_sem:
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
    # deepcopy+summary 재계산(filter_snapshot_for_user)은 스냅샷이 크면 ms 단위
    # CPU 작업이고 클라이언트 수×폴링 주기만큼 반복되므로 이벤트 루프에서 직접
    # 돌리지 않는다 — 단일 루프가 막히면 모든 응답·프로브가 같이 밀린다.
    view = await asyncio.to_thread(
        filter_snapshot_for_user, snap, access, st.hide_internal, ref_seed)
    return JSONResponse(view)


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
async def healthz() -> JSONResponse:
    """Liveness — 프로세스가 살아 있으면 항상 200(수집 상태와 무관)."""
    return JSONResponse({"status": "ok"})


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Readiness — 첫 스냅샷을 아직 한 번도 만들지 못했으면 503.

    기존엔 /healthz 와 같은 핸들러로 무조건 200 이라, LiteLLM 미도달·k8s 인증
    실패로 첫 수집이 영원히 실패해도 Pod 가 Ready 로 마킹돼 'loading' 빈 화면을
    서빙했다. **보수적으로** 첫 수집 완료 전(loading)에만 503 을 준다 — 일단
    한 번이라도 스냅샷이 생기면, 이후 백그라운드 수집이 실패하거나 데이터가
    낡아도 200 을 유지한다(가용성 우선; staleness 는 메트릭/대시보드로 노출).
    """
    snap = await request.app.state.store.get()
    if snap.get("loading"):
        return JSONResponse({"status": "loading"}, status_code=503)
    return JSONResponse({"status": "ok"})
