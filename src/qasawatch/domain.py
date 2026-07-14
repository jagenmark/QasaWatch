"""Transport-neutral domain values and provider contracts.

Provider implementations deliberately exchange immutable values rather than ORM
objects.  This keeps sessions and transactions inside the core service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, AsyncIterator, Mapping, Protocol, runtime_checkable


class ListingStage(StrEnum):
    DISCOVERED = "discovered"
    ENRICHED = "enriched"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DeliveryChannel(StrEnum):
    SHEETS = "sheets"
    DISCORD = "discord"
    EMAIL = "email"


class DeliveryState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"
    SKIPPED = "skipped"


class RunKind(StrEnum):
    WATCHER = "watcher"
    MANUAL = "manual"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReasonSource(StrEnum):
    MACHINE = "machine"
    HUMAN = "human"


@dataclass(frozen=True, slots=True)
class RawListing:
    provider: str
    url: str
    external_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EnrichedListing:
    provider: str
    url: str
    external_id: str | None
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ListingSnapshot:
    id: int
    provider: str
    url: str
    external_id: str | None
    stage: ListingStage
    data: Mapping[str, Any]
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class RejectionReason:
    code: str
    message: str
    source: ReasonSource = ReasonSource.MACHINE
    rule: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FilterDecision:
    accepted: bool
    reasons: tuple[RejectionReason, ...] = ()

    def __post_init__(self) -> None:
        if self.accepted and self.reasons:
            raise ValueError("an accepted decision cannot contain rejection reasons")
        if not self.accepted and not self.reasons:
            raise ValueError("a rejected decision requires at least one reason")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    provider_message_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DiscoveryProvider(Protocol):
    name: str

    def discover(self) -> AsyncIterator[RawListing]: ...


@runtime_checkable
class EnrichmentProvider(Protocol):
    name: str

    async def enrich(self, listing: RawListing) -> EnrichedListing: ...


@runtime_checkable
class DeliveryProvider(Protocol):
    channel: DeliveryChannel

    async def deliver(
        self, listing: ListingSnapshot, *, idempotency_key: str
    ) -> DeliveryResult: ...
