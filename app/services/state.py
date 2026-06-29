"""백그라운드 스냅샷 수집 상태 + 리프레셔.

요청 경로에서 수집하지 않는다(특히 LiteLLM /health 는 느림). 백그라운드 asyncio
태스크가 주기적으로 스냅샷을 다시 만들어 캐시에 넣고, HTTP 핸들러는 마지막
캐시 스냅샷을 즉시 돌려준다 -> 어떤 요청도 수집에 블로킹되지 않는다.

수집기 자체는 동기(blocking urllib)라서 asyncio.to_thread 로 이벤트 루프 밖에서
돌린다(원래 daemon 스레드 두 개 = refresh_loop + health_loop 를 대체).
"""

import asyncio

from app import __version__
from app.services.demo import demo_snapshot
from app.services.litellm import fetch_health
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

    async def collect_once(self):
        """스냅샷 1회 수집(메인은 health 없이 빠르게) 후 비동기 health 주입."""
        if self.demo:
            snap = await asyncio.to_thread(demo_snapshot)
            await self.store.set(snap, None)
            return snap

        snap = await asyncio.to_thread(build_snapshot, self.settings, False)
        if snap.get("litellm"):
            async with self._health_lock:
                h = self._health
            if h is not None:
                # 비동기로 받아둔 /health 를 주입하고 status/summary 재계산
                snap["litellm"]["health"] = h
                snap["litellm"]["deployments"] = merge_deployments_with_health(
                    snap["litellm"])
                snap["summary"] = summarize(snap)
        await self.store.set(snap, None)
        return snap

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await self.store.set_error("%s: %s" % (type(e).__name__, e))

    async def _health_loop(self):
        # 느린 /health 를 천천히(>=30s) 따로 수집. 도착하면 다음 collect 에 반영됨.
        url = self.settings.get("litellm_url")
        key = self.settings.get("api_key")
        ht = self.settings.get("health_timeout", 90.0)
        while True:
            try:
                h = await asyncio.to_thread(fetch_health, url, key, ht)
                if h is not None:
                    async with self._health_lock:
                        self._health = h
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(max(30.0, self.interval))

    async def start(self):
        """첫 스냅샷을 동기로 1회 채운 뒤(즉시 화면에 데이터), 백그라운드 루프 가동."""
        try:
            await self.collect_once()
        except Exception as e:  # noqa: BLE001
            await self.store.set_error("%s: %s" % (type(e).__name__, e))

        self._tasks.append(asyncio.create_task(self._refresh_loop()))
        # health 수집은 데모가 아니고 health 가 켜져 있으며 litellm_url 이 있을 때만
        if (not self.demo and self.settings.get("health", True)
                and self.settings.get("litellm_url")):
            self._tasks.append(asyncio.create_task(self._health_loop()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
