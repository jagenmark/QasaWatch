from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from qasawatch.config import ConfigStore
from qasawatch.db import Database
from qasawatch.models import SchemaVersion
from qasawatch.secrets import SecretRef


@pytest.fixture
async def database(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    yield db
    await db.dispose()


async def test_initialize_records_schema_and_config_keeps_secret_reference(database):
    async with database.sessions() as session:
        assert await session.scalar(select(func.max(SchemaVersion.version))) == 2

    config = ConfigStore(database)
    await config.set_value("watcher.enabled", True)
    await config.set_secret("discord.token", "env:QASAWATCH_DISCORD_TOKEN")

    assert await config.get("watcher.enabled") is True
    assert await config.get("discord.token") == SecretRef("env", "QASAWATCH_DISCORD_TOKEN")
    assert await config.get(
        "discord.token",
        resolve_secret=True,
        resolver=type("Resolver", (), {"resolve": lambda self, ref: f"resolved:{ref.name}"})(),
    ) == "resolved:QASAWATCH_DISCORD_TOKEN"


async def test_scan_lease_prevents_overlap_but_can_take_over_when_stale(database):
    now = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)
    first = await database.acquire_scan_lease(
        "qasa", "worker-a", ttl=timedelta(minutes=5), now=now
    )
    overlap = await database.acquire_scan_lease(
        "qasa", "worker-b", ttl=timedelta(minutes=5), now=now + timedelta(minutes=1)
    )
    takeover = await database.acquire_scan_lease(
        "qasa", "worker-b", ttl=timedelta(minutes=5), now=now + timedelta(minutes=6)
    )

    assert first.acquired
    assert not overlap.acquired
    assert takeover.acquired
    assert not await database.release_scan_lease("qasa", "worker-a")
    assert await database.release_scan_lease("qasa", "worker-b")
