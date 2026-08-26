"""백그라운드 스냅샷 수집 상태 + 리프레셔.

요청 경로에서 수집하지 않는다(특히 LiteLLM /health 는 느림). 백그라운드 asyncio
태스크가 주기적으로 스냅샷을 다시 만들어 캐시에 넣고, HTTP 핸들러는 마지막
캐시 스냅샷을 즉시 돌려준다 -> 어떤 요청도 수집에 블로킹되지 않는다.

수집기 자체는 동기(blocking urllib)라서 asyncio.to_thread 로 이벤트 루프 밖에서
돌린다(원래 daemon 스레드 두 개 = refresh_loop + health_loop 를 대체).
"""

import asyncio
import time

from app import __version__
from app.services.demo import demo_snapshot
from app.services.litellm import (
    _deployment_health_safe,
    _strip_openai_suffix,
    aggregate_selective_health,
    fetch_health,
    fetch_health_for_model,
    health_check_allowed_bases,
    select_health_check_models,
)
from app.services.load import (
    aggregate_targets,
    attach_load_to_deployments,
    collect_load_via_prometheus,
    load_targets,
    merge_load_sources,
    probe_pod_load,
)
from app.services.snapshot import (
    build_snapshot,
    merge_deployments_with_health,
    summarize,
)


class SnapshotStore:
    """마지막으로 수집한 스냅샷 + 수집 오류를 보관하는 스레드/태스크 세이프 캐시."""

    def __init__(self):
        self._snap = None
        self._err = None
        self._lock = asyncio.Lock()

    async def set(self, snap, err=None):
        async with self._lock:
            self._snap = snap
            self._err = err

    async def set_error(self, err):
        async with self._lock:
            self._err = err

    async def get(self):
        """캐시된 스냅샷(없으면 loading, 수집오류면 collect_error 부착)."""
        async with self._lock:
            snap, err = self._snap, self._err
        if snap is None:
            return {"version": __version__, "loading": True,
                    "error": err, "summary": {}, "litellm": None}
        if err:
            return dict(snap, collect_error=err)
        return snap


class Refresher:
    """주기적 스냅샷 수집 + 느린 /health 별도 수집을 관리하는 백그라운드 러너."""

    def __init__(self, settings, store, interval, demo=False):
        self.settings = settings
        self.store = store
        self.interval = max(1.0, interval)
        self.demo = demo
        self._health = None
        self._health_lock = asyncio.Lock()
        self._tasks = []
        # 노드 GPU 장치명 라벨(nvidia.com/gpu.product)은 노드 수명 동안 불변이므로
        # 사이클(기본 5s)마다 K8sClient 를 새로 만들어도 이 캐시는 유지해, 정적
        # 라벨을 위해 Node 오브젝트를 반복해서 받지 않는다(공유 k8s API 부하 절감).
        self._node_cache = {}
        # 거의 변하지 않는 k8s 조회(ISVC 부재 · Service selector)의 TTL 캐시.
        # node_cache 와 같은 이유로 프로세스 수명이다 — 사이클마다 새로
        # 만들면(bc_cache 처럼) 사이클 간 절감이 0 이다.
        self._meta_cache = {}
        # 지금 부하(엔진 게이지). health 와 같은 계약: None 을 주면 직전 값을 유지해
        # 실패 라운드가 마지막 정상 결과를 지우지 않는다.
        self._load = None
        self._load_alias = {}
        self._load_lock = asyncio.Lock()
        # 생성 토큰 카운터 차분용 — 사이클 간 유지돼야 tok/s 가 나온다
        # (node_cache 와 같은 이유로 프로세스 수명).
        self._tput_cache = {}

    async def collect_once(self):
        """스냅샷 1회 수집(메인은 health 없이 빠르게) 후 비동기 health 주입."""
        if self.demo:
            snap = await asyncio.to_thread(demo_snapshot)
            await self.store.set(snap, None)
            return snap

        snap = await asyncio.to_thread(
            build_snapshot, self.settings, False, self._node_cache,
            self._meta_cache)
        if snap.get("litellm"):
            async with self._health_lock:
                h = self._health
            if h is not None:
                # 비동기로 받아둔 /health 를 주입하고 status/summary 재계산
                snap["litellm"]["health"] = h
                # 선택적 health 의 조회 실패/경고는 대시보드가 읽는 litellm.errors
                # 로 노출한다(health dict 안에만 두면 아무 렌더러도 안 읽어서
                # 체계적 실패가 조용히 '?' 폴백으로 묻힌다). 홍수 방지 3건 캡.
                herrs = h.get("errors") or []
                if herrs:
                    dst = snap["litellm"].setdefault("errors", [])
                    dst.extend(herrs[:3])
                    if len(herrs) > 3:
                        dst.append("selective health: 외 %d건 실패"
                                   % (len(herrs) - 3))
                snap["litellm"]["deployments"] = merge_deployments_with_health(
                    snap["litellm"])
                snap["summary"] = summarize(snap)
            # 비동기로 모아둔 '지금 부하'를 주입 — health 와 같은 이유로 별도
            # 주기다(Pod 팬아웃이라 수집 사이클에 넣으면 갱신이 늘어진다).
            async with self._load_lock:
                loads, alias = self._load, self._load_alias
            if loads:
                snap["litellm"]["deployments"] = attach_load_to_deployments(
                    snap["litellm"]["deployments"], loads,
                    _strip_openai_suffix, alias)
                snap["load_enabled"] = True
                snap["summary"] = summarize(snap)
        await self.store.set(snap, None)
        return snap

    # 연속 수집 **예외** 시 지수 백오프 상한. 예외 경로는 예상 밖 결함(버그/자원
    # 고갈)이라 즉시 재시도해도 같은 이유로 실패할 확률이 높다 — 5초 타이트 재시도로
    # CPU(200m 캡)와 로그를 태우지 않는다. 정상적인 수집 실패(LiteLLM 미도달 등)는
    # 예외가 아니라 snap.errors 로 기록되므로 이 백오프의 대상이 아니다.
    _BACKOFF_MAX = 60.0

    def _next_delay(self, failures):
        """다음 사이클까지 대기 — 연속 예외 failures 회면 interval×2^n (상한 60s)."""
        if failures <= 0:
            return self.interval
        return min(self.interval * (2 ** min(failures, 6)),
                   max(self._BACKOFF_MAX, self.interval))

    async def _refresh_loop(self):
        failures = 0
        while True:
            await asyncio.sleep(self._next_delay(failures))
            try:
                await self.collect_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                failures += 1
                await self.store.set_error("%s: %s" % (type(e).__name__, e))

    # 선택적 health 동시 조회 수 — main.py 의 수집 스레드 예산(_COLLECT_THREADS=8)
    # 을 공유하므로 절반만 쓴다(빌드/유저뷰 조회와 경합 방지). 자체 스레드풀 금지.
    _SELECTIVE_PARALLEL = 4
    # 모델 1개 조회 타임아웃 상한 — health_timeout(기본 90s)은 "전 백엔드 동시
    # ping" 기준이라 개별 1콜엔 과하다. 그대로 쓰면 걸린 백엔드 몇 개에 한 회전이
    # 분 단위로 늘어져 상태가 낡는다.
    _SELECTIVE_CALL_TIMEOUT = 30.0

    async def _health_loop(self, fetch_once):
        """느린 health 수집 공통 루프(>=30s 주기) — 전량/선택 모드가 공유한다.

        fetch_once() 가 dict 를 주면 _health 교체, None 이면 유지 — 실패 라운드가
        마지막 정상 결과를 빈 결과로 덮어쓰지 않는다(전량 fetch_health 는 실패 시
        None, 선택 aggregate 는 전 모델 실패 시 None 을 주는 공통 계약).
        """
        while True:
            try:
                h = await fetch_once()
                if h is not None:
                    async with self._health_lock:
                        self._health = h
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(max(30.0, self.interval))

    async def _fetch_full_health(self):
        """전량 /health 1회 (LiteLLM 이 모든 백엔드를 실제 ping — 느림)."""
        return await asyncio.to_thread(
            fetch_health, self.settings.get("litellm_url"),
            self.settings.get("api_key"),
            self.settings.get("health_timeout", 90.0))

    async def _fetch_selective_health(self):
        """선택적 health 1회: 안전 판정된 모델만 /health?model= 병렬 조회.

        전량 /health(모든 백엔드 ping → scale-to-zero 를 깨움) 대신, 최신 스냅샷의
        k8s 판정으로 '찔러도 안전한 모델'만 개별 조회한다. 결과는 기존 /health
        모양이라 collect_once 의 동일 주입 경로를 탄다 — 체크한 모델만
        UP/DOWN(health), 나머지는 지금처럼 k8s 폴백(→ '?').
        """
        url = self.settings.get("litellm_url")
        key = self.settings.get("api_key")
        ht = min(self.settings.get("health_timeout", 90.0),
                 self._SELECTIVE_CALL_TIMEOUT)
        snap = await self.store.get()
        deps = ((snap.get("litellm") or {}).get("deployments")) or []
        if not deps:
            return None   # 첫 스냅샷 수집 전 — 아무것도 바꾸지 않음
        names = select_health_check_models(deps)
        if not names:
            # 체크 대상 없음 → 빈 집계를 주입해 "아무것도 체크 안 함"을 정직하게
            # 반영(전부 k8s 폴백). k8s 판정 필드 자체가 없으면 backend_count
            # 비활성/권한 없음 — 조용한 무력화가 되지 않게 경고를 남긴다.
            h = aggregate_selective_health([])
            if not any("network_type" in d for d in deps):
                h["errors"].append(
                    "selective health: deployment 에 k8s 판정(network_type)이 "
                    "없어 체크 대상을 못 고름 — backend_count 비활성/권한 확인")
            return h
        # ?model= 응답 검증용: 모델별 허용 api_base(접미어 제거) 집합.
        # 조회 대상(names)뿐 아니라 '일시중지라서만 빠진 안전한 backend' 도
        # 포함된다 — 판정 규칙은 health_check_allowed_bases 참고.
        allowed = health_check_allowed_bases(deps, names)
        sem = asyncio.Semaphore(self._SELECTIVE_PARALLEL)

        async def one(name):
            async with sem:
                ok, data, err = await asyncio.to_thread(
                    fetch_health_for_model, url, key, name, ht)
                return (name, ok, data, err)

        results = await asyncio.gather(*(one(n) for n in names))
        return aggregate_selective_health(list(results), allowed)

    # 부하 조회 동시 실행 수 — 선택적 health 와 같은 이유로 공용 수집 스레드
    # 예산(_COLLECT_THREADS=8)의 절반만 쓴다. 자체 스레드풀 금지.
    _LOAD_PARALLEL = 4

    async def _fetch_load(self):
        """지금 부하 1회: Pod 마다 /metrics 를 병렬 조회해 base 별로 집계.

        Pod 주소는 **직전 스냅샷**에서 읽는다(선택적 health 와 같은 방식) — 이미
        backend_count 가 GPU 집계와 같은 Pod 목록에서 얻어둔 값이라 k8s 재조회가 없다.
        전 대상이 실패하면 None 을 돌려 직전 값을 유지한다.
        """
        snap = await self.store.get()
        deps = ((snap.get("litellm") or {}).get("deployments")) or []
        if not deps:
            return None    # 첫 스냅샷 전 — 아무것도 바꾸지 않음
        targets, alias = load_targets(deps, _strip_openai_suffix,
                                      _deployment_health_safe)
        jobs = [(base, url) for base, spec in targets.items()
                for url in (spec.get("urls") or [])]
        timeout = self.settings.get("load_timeout", 3.0)
        now = time.monotonic()
        sem = asyncio.Semaphore(self._LOAD_PARALLEL)

        async def one(base, url):
            async with sem:
                return base, await asyncio.to_thread(
                    probe_pod_load, url, timeout, now, self._tput_cache)

        samples = {}
        for base, sample in await asyncio.gather(*(one(b, u) for b, u in jobs)):
            samples.setdefault(base, []).append(sample)
        loads = aggregate_targets(targets, samples,
                                  self.settings.get("load_thresholds"))
        if self.settings.get("prometheus_url"):
            # Pod 직접 조회가 막힌 대상만 Prometheus 로 보강하고, **표본이 더
            # 많을 때만** 교체한다(폴백이 원래 값을 더 나쁘게 만들지 않게).
            weak = set(b for b, l in loads.items()
                       if l.get("state") == "unknown"
                       or (l.get("pods_failed") or 0) > 0)
            if weak:
                prom = await asyncio.to_thread(
                    collect_load_via_prometheus, targets, self.settings, now,
                    weak, self._tput_cache)
                loads = merge_load_sources(loads, prom)
        if not any(l.get("state") != "unknown" for l in loads.values()):
            return None    # 전부 모름 — 직전 값 유지
        return loads, alias

    async def _load_loop(self):
        """부하 수집 루프. 주기는 refresh interval — "지금 바쁜가"는 빨리 낡는다."""
        while True:
            try:
                got = await self._fetch_load()
                if got is not None:
                    async with self._load_lock:
                        self._load, self._load_alias = got
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.interval)

    async def start(self):
        """첫 스냅샷을 동기로 1회 채운 뒤(즉시 화면에 데이터), 백그라운드 루프 가동."""
        try:
            await self.collect_once()
        except Exception as e:  # noqa: BLE001
            await self.store.set_error("%s: %s" % (type(e).__name__, e))

        self._tasks.append(asyncio.create_task(self._refresh_loop()))
        # health 수집은 데모가 아니고 litellm_url 이 있을 때만.
        # 우선순위: 전량 /health(MONITOR_HEALTH=true) > 선택적(MONITOR_SELECTIVE_HEALTH=true).
        # 둘 다 켜져 있으면 전량이 이미 모든 모델을 커버하므로 선택적 루프는 안 띄운다.
        # 전량의 기본은 off(config.py 와 동일) — 모든 백엔드를 실 ping 하는 부하 모드는
        # 명시적으로만 켠다(scale-to-zero 각성 방지).
        if not self.demo and self.settings.get("litellm_url"):
            if self.settings.get("health", False):
                self._tasks.append(asyncio.create_task(
                    self._health_loop(self._fetch_full_health)))
            elif self.settings.get("selective_health", True):
                self._tasks.append(asyncio.create_task(
                    self._health_loop(self._fetch_selective_health)))
            # 지금 부하(엔진 게이지) — Pod 주소가 있는 백엔드만 직접 조회한다.
            if self.settings.get("load", True):
                self._tasks.append(asyncio.create_task(self._load_loop()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
