"""Async SQLite database lifecycle, first-version migration, and scan leases."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .domain import DeliveryChannel, DeliveryState, RunStatus
from .models import (
    Base,
    DeliveryAttempt,
    EmailBatch,
    ListingDelivery,
    Run,
    ScanLease,
    SchemaVersion,
)

SCHEMA_VERSION = 2


class UnsupportedSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LeaseResult:
    acquired: bool
    name: str
    owner: str
    expires_at: datetime


class Database:
    def __init__(self, url_or_path: str | Path = "qasawatch.db", *, echo: bool = False) -> None:
        value = str(url_or_path)
        if "://" not in value:
            path = Path(value).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            value = f"sqlite+aiosqlite:///{path.as_posix()}"
        self.url = value
        self.engine: AsyncEngine = create_async_engine(value, echo=echo)
        self.sessions = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        if value.startswith("sqlite"):
            self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def initialize(self) -> None:
        """Create the current schema, migrate v1 additively, reject newer DBs.

        This explicit version row is the v1 migration mechanism.  Future releases
        can apply ordered migrations before inserting their version row.
        """

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions.begin() as session:
            current = await session.scalar(select(func.max(SchemaVersion.version)))
            if current is None:
                session.add(SchemaVersion(version=SCHEMA_VERSION))
            elif current > SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            elif current == 1:
                # v2 adds only email_batches/email_batch_listings. create_all
                # above is the idempotent DDL migration for these new tables.
                session.add(SchemaVersion(version=SCHEMA_VERSION))
            elif current < SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"database schema {current} requires a migration to {SCHEMA_VERSION}"
                )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def recover_interrupted_work(self, *, at: datetime | None = None) -> None:
        """Make interrupted work runnable without discarding idempotency keys.

        Sheets attempts retain their stable key and return to pending because a
        committed row can be detected remotely. Webhook/SMTP attempts become
        ``manual_review`` because those transports cannot guarantee that an
        acknowledgement loss is safe to resend.
        """

        now = at or datetime.now(UTC)
        async with self.sessions.begin() as session:
            # Sheets can detect a committed row by its stable key and safely
            # resume. Webhooks and SMTP do not guarantee remote idempotency, so
            # a crash after send begins requires an operator decision.
            await session.execute(
                update(ListingDelivery)
                .where(
                    ListingDelivery.state == DeliveryState.IN_PROGRESS.value,
                    ListingDelivery.channel == DeliveryChannel.SHEETS.value,
                )
                .values(state=DeliveryState.PENDING.value, updated_at=now)
            )
            ambiguous_ids = select(ListingDelivery.id).where(
                ListingDelivery.state == DeliveryState.IN_PROGRESS.value,
                ListingDelivery.channel.in_(
                    (DeliveryChannel.DISCORD.value, DeliveryChannel.EMAIL.value)
                ),
            )
            await session.execute(
                update(DeliveryAttempt)
                .where(
                    DeliveryAttempt.delivery_id.in_(ambiguous_ids),
                    DeliveryAttempt.state == DeliveryState.IN_PROGRESS.value,
                )
                .values(
                    state=DeliveryState.MANUAL_REVIEW.value,
                    error="process interrupted after delivery began",
                    finished_at=now,
                )
            )
            await session.execute(
                update(ListingDelivery)
                .where(ListingDelivery.id.in_(ambiguous_ids))
                .values(
                    state=DeliveryState.MANUAL_REVIEW.value,
                    last_error="process interrupted after delivery began",
                    updated_at=now,
                )
            )
            # A process crash while SMTP was sending is unknowable: retrying can
            # duplicate mail, so require an operator decision.
            await session.execute(
                update(EmailBatch)
                .where(EmailBatch.state == "sending")
                .values(state="manual_review", last_error="process interrupted during SMTP send", updated_at=now)
            )
            await session.execute(
                update(Run)
                .where(Run.status == RunStatus.RUNNING.value)
                .values(
                    status=RunStatus.FAILED.value,
                    finished_at=now,
                    error="process interrupted before run completion",
                )
            )

    async def acquire_scan_lease(
        self,
        name: str,
        owner: str,
        *,
        ttl: timedelta = timedelta(minutes=10),
        now: datetime | None = None,
    ) -> LeaseResult:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        timestamp = now or datetime.now(UTC)
        expires_at = timestamp + ttl
        async with self.sessions.begin() as session:
            await session.execute(
                sqlite_insert(ScanLease)
                .values(
                    name=name,
                    owner=owner,
                    acquired_at=timestamp,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(index_elements=[ScanLease.name])
            )
            result = await session.execute(
                update(ScanLease)
                .where(
                    ScanLease.name == name,
                    (ScanLease.owner == owner) | (ScanLease.expires_at <= timestamp),
                )
                .values(owner=owner, acquired_at=timestamp, expires_at=expires_at)
            )
            acquired = result.rowcount == 1
        return LeaseResult(acquired, name, owner, expires_at)

    async def release_scan_lease(self, name: str, owner: str) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                delete(ScanLease).where(ScanLease.name == name, ScanLease.owner == owner)
            )
            return result.rowcount == 1

    async def renew_scan_lease(
        self,
        name: str,
        owner: str,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> bool:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        timestamp = now or datetime.now(UTC)
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(ScanLease)
                .where(ScanLease.name == name, ScanLease.owner == owner)
                .values(expires_at=timestamp + ttl)
            )
            return result.rowcount == 1
