import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from qasawatch.config import ConfigStore
from qasawatch.db import Database
from qasawatch.scheduler import WatchScheduler


async def test_scheduler_prevents_local_overlap(tmp_path):
    db = Database(tmp_path / "state.db"); await db.initialize()
    await ConfigStore(db).set_value("watcher.config", {"enabled": True})
    started, release = asyncio.Event(), asyncio.Event()
    async def run_callback(**kwargs):
        started.set(); await release.wait(); return {"status": "succeeded"}
    scheduler = WatchScheduler(db, ConfigStore(db), run_callback)
    first = asyncio.create_task(scheduler.run_once())
    await started.wait()
    assert (await scheduler.run_once())["status"] == "overlap"
    release.set(); assert (await first)["status"] == "succeeded"
    assert await scheduler.next_run() is not None
    await db.dispose()


async def test_denied_database_lease_advances_next_run(tmp_path):
    db = Database(tmp_path / "denied.db")
    await db.initialize()
    await ConfigStore(db).set_value("watcher.config", {"enabled": True})
    await db.acquire_scan_lease("watcher-scan", "other", ttl=timedelta(minutes=5))
    scheduler = WatchScheduler(db, ConfigStore(db), lambda **kwargs: None)
    result = await scheduler.run_once()
    assert result["status"] == "overlap"
    assert await scheduler.next_run() is not None
    await db.dispose()


async def test_long_scan_renews_cross_process_lease(tmp_path):
    db = Database(tmp_path / "heartbeat.db")
    await db.initialize()
    started, release = asyncio.Event(), asyncio.Event()

    async def run_callback(**kwargs):
        started.set()
        await release.wait()
        return {"status": "succeeded"}

    scheduler = WatchScheduler(
        db,
        ConfigStore(db),
        run_callback,
        lease_ttl=timedelta(milliseconds=120),
    )
    task = asyncio.create_task(scheduler.run_once())
    await started.wait()
    await asyncio.sleep(0.2)
    intruder = await db.acquire_scan_lease(
        "watcher-scan", "intruder", ttl=timedelta(seconds=1)
    )
    assert not intruder.acquired
    release.set()
    await task
    await db.dispose()


async def test_failed_scheduled_scan_does_not_kill_scheduler_loop(tmp_path):
    db = Database(tmp_path / "failure.db")
    await db.initialize()
    store = ConfigStore(db)
    await store.set_value(
        "watcher.config",
        {
            "enabled": True,
            "base_interval_minutes": 1,
            "jitter_minutes": 0,
            "destinations": [
                {"label": "one", "address": "One, Stockholm"},
                {"label": "two", "address": "Two, Stockholm"},
            ],
        },
    )
    await store.set_value(
        "scheduler.next_run", (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    )
    attempted = asyncio.Event()

    async def fail(**kwargs):
        attempted.set()
        raise RuntimeError("temporary scan failure")

    scheduler = WatchScheduler(db, store, fail)
    await scheduler.start()
    await asyncio.wait_for(attempted.wait(), timeout=1)
    await asyncio.sleep(0.02)
    assert scheduler._task is not None and not scheduler._task.done()
    assert scheduler.last_error == "RuntimeError: temporary scan failure"
    assert await scheduler.next_run() > datetime.now(UTC).astimezone()
    await scheduler.stop()
    await db.dispose()


async def test_disabled_config_clears_persisted_next_run(tmp_path):
    db = Database(tmp_path / "disabled.db")
    await db.initialize()
    store = ConfigStore(db)
    await store.set_value("watcher.config", {"enabled": True})
    scheduler = WatchScheduler(db, store, lambda **kwargs: None)
    await scheduler.schedule_next()
    assert await scheduler.next_run() is not None

    await store.set_value("watcher.config", {"enabled": False})
    await scheduler.config_changed()

    assert await scheduler.next_run() is None
    await db.dispose()


async def test_lease_loss_cancels_active_scan_callback(tmp_path):
    db = Database(tmp_path / "lost-lease.db")
    await db.initialize()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def long_scan(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scheduler = WatchScheduler(
        db,
        ConfigStore(db),
        long_scan,
        lease_ttl=timedelta(milliseconds=90),
    )
    scan = asyncio.create_task(scheduler.run_once())
    await started.wait()
    assert await db.release_scan_lease("watcher-scan", scheduler.owner)

    with pytest.raises(RuntimeError, match="lease ownership was lost"):
        await asyncio.wait_for(scan, timeout=1)
    assert cancelled.is_set()
    assert not scheduler.running
    await db.dispose()


async def test_config_wake_is_not_lost_between_read_and_wait(tmp_path):
    class PausingStore(ConfigStore):
        def __init__(self, database):
            super().__init__(database)
            self.read_snapshot = asyncio.Event()
            self.release_snapshot = asyncio.Event()
            self.first = True

        async def get(self, key, default=None, **kwargs):
            value = await super().get(key, default, **kwargs)
            if key == "watcher.config" and self.first:
                self.first = False
                self.read_snapshot.set()
                await self.release_snapshot.wait()
            return value

    db = Database(tmp_path / "wake-race.db")
    await db.initialize()
    store = PausingStore(db)
    await store.set_value("watcher.config", {"enabled": False})
    ran = asyncio.Event()

    async def callback(**kwargs):
        ran.set()
        return {"status": "succeeded"}

    scheduler = WatchScheduler(db, store, callback)
    await scheduler.start()
    await store.read_snapshot.wait()
    plain_store = ConfigStore(db)
    await plain_store.set_value(
        "watcher.config",
        {
            "enabled": True,
            "base_interval_minutes": 1,
            "jitter_minutes": 0,
            "destinations": [
                {"label": "one", "address": "One, Stockholm"},
                {"label": "two", "address": "Two, Stockholm"},
            ],
        },
    )
    await plain_store.set_value(
        "scheduler.next_run", (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    )
    scheduler._wake.set()
    store.release_snapshot.set()

    await asyncio.wait_for(ran.wait(), timeout=1)
    await scheduler.stop()
    await db.dispose()
