"""Application bootstrap settings and database-backed runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .db import Database
from .models import ConfigRecord
from .secrets import EnvironmentSecretResolver, SecretRef, SecretResolver


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    """Only bootstrap values live outside the DB; secrets remain references."""

    database: str = "qasawatch.db"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "BootstrapSettings":
        return cls(
            database=os.getenv("QASAWATCH_DATABASE", "qasawatch.db"),
            log_level=os.getenv("QASAWATCH_LOG_LEVEL", "INFO").upper(),
        )


class ConfigStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def set_value(self, key: str, value: Any) -> None:
        self._validate_key(key)
        await self._upsert(ConfigRecord(key=key, value=value, secret_ref=None))

    async def set_secret(self, key: str, reference: SecretRef | str) -> None:
        self._validate_key(key)
        parsed = SecretRef.parse(reference) if isinstance(reference, str) else reference
        await self._upsert(ConfigRecord(key=key, value=None, secret_ref=str(parsed)))

    async def _upsert(self, record: ConfigRecord) -> None:
        async with self.database.sessions.begin() as session:
            existing = await session.get(ConfigRecord, record.key)
            if existing is None:
                session.add(record)
            else:
                existing.value = record.value
                existing.secret_ref = record.secret_ref

    async def get(
        self,
        key: str,
        default: Any = None,
        *,
        resolve_secret: bool = False,
        resolver: SecretResolver | None = None,
    ) -> Any:
        async with self.database.sessions() as session:
            record = await session.scalar(select(ConfigRecord).where(ConfigRecord.key == key))
        if record is None:
            return default
        if record.secret_ref is None:
            return record.value
        reference = SecretRef.parse(record.secret_ref)
        if not resolve_secret:
            return reference
        return (resolver or EnvironmentSecretResolver()).resolve(reference)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or len(key) > 255:
            raise ValueError("configuration key must contain 1..255 characters")
