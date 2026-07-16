"""Europe/Stockholm scheduler with persisted next-run state and DB leases."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ConfigStore
from .db import Database
from .redaction import redact_text

STOCKHOLM = ZoneInfo("Europe/Stockholm")


class WatchScheduler:
    def __init__(self, database: Database, config_store: ConfigStore, run_callback, *, random_source=None, lease_ttl=timedelta(minutes=30)) -> None:
        self.database = database
        self.config_store = config_store
        self.run_callback = run_callback
        self.random = random_source or random.Random()
        self.lease_ttl = lease_ttl
        self.owner = f"scheduler-{uuid.uuid4().hex}"
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self.running = False
        self.lease_healthy = True
        self.last_error: str | None = None

    async def next_run(self) -> datetime | None:
        value = await self.config_store.get("scheduler.next_run")
        if not value:
            return None
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=STOCKHOLM)
        return parsed.astimezone(STOCKHOLM)

    async def schedule_next(self, *, now: datetime | None = None) -> datetime:
        config = await self._config()
        local_now = (now or datetime.now(STOCKHOLM)).astimezone(STOCKHOLM)
        jitter = self.random.uniform(-config.jitter_minutes, config.jitter_minutes)
        delay = max(1.0, config.base_interval_minutes + jitter)
        next_at = local_now + timedelta(minutes=delay)
        await self.config_store.set_value("scheduler.next_run", next_at.isoformat())
        return next_at

    async def _reschedule_if_enabled(self) -> datetime | None:
        config = await self._config()
        if not config.enabled:
            await self.config_store.set_value("scheduler.next_run", None)
            return None
        return await self.schedule_next()

    async def run_once(self, *, reason: str = "scheduled"):
        if self._run_lock.locked():
            return {"status": "overlap", "reason": reason}
        async with self._run_lock:
            lease = await self.database.acquire_scan_lease("watcher-scan", self.owner, ttl=self.lease_ttl)
            if not lease.acquired:
                await self._reschedule_if_enabled()
                return {"status": "overlap", "reason": reason, "lease_expires_at": lease.expires_at.isoformat()}
            self.running = True
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(), name="qasawatch-lease-heartbeat"
            )
            callback = asyncio.create_task(
                self.run_callback(reason=reason, owner=self.owner),
                name="qasawatch-scan-callback",
            )
            try:
                done, _ = await asyncio.wait(
                    (callback, heartbeat), return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    # Lease ownership is the authority to scan. Stop the active
                    # callback before allowing the heartbeat failure to escape.
                    callback.cancel()
                    await asyncio.gather(callback, return_exceptions=True)
                    heartbeat.result()
                result = await callback
                self.last_error = None
                return result
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {redact_text(exc)}"[:1000]
                raise
            finally:
                callback.cancel()
                await asyncio.gather(callback, return_exceptions=True)
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                self.running = False
                await self.database.release_scan_lease("watcher-scan", self.owner)
                await self._reschedule_if_enabled()

    async def run_now(self):
        result = await self.run_once(reason="manual-run-now")
        self._wake.set()
        return result

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="qasawatch-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def config_changed(self) -> None:
        self._wake.set()
        await self._reschedule_if_enabled()

    async def _config(self):
        from .schemas import WatcherConfig
        return WatcherConfig.model_validate(await self.config_store.get("watcher.config", {}))

    async def _loop(self) -> None:
        while True:
            # Clear before observing configuration/target. Any later change is
            # retained and wakes the wait based on that observation.
            self._wake.clear()
            config = await self._config()
            if not config.enabled:
                if not self._wake.is_set():
                    await self.config_store.set_value("scheduler.next_run", None)
                await self._wake.wait()
                continue
            target = await self.next_run() or await self.schedule_next()
            delay = max(0, (target.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                try:
                    await self.run_once()
                except Exception:
                    # The service records scan/output failures durably. Keep the
                    # unattended scheduling loop alive for the bounded next run.
                    continue

    async def _lease_heartbeat(self) -> None:
        interval = max(0.01, min(60.0, self.lease_ttl.total_seconds() / 3))
        self.lease_healthy = True
        while True:
            await asyncio.sleep(interval)
            renewed = await self.database.renew_scan_lease(
                "watcher-scan", self.owner, ttl=self.lease_ttl
            )
            if not renewed:
                self.lease_healthy = False
                raise RuntimeError("watcher scan lease ownership was lost")
