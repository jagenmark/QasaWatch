"""Composable listing filters with structured, auditable rejection reasons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Protocol

from .domain import EnrichedListing, FilterDecision, ReasonSource, RejectionReason


class ListingFilter(Protocol):
    name: str

    async def evaluate(self, listing: EnrichedListing) -> RejectionReason | None: ...


class FilterChain:
    """Runs every filter so the user sees all reasons, in stable rule order."""

    def __init__(self, filters: Iterable[ListingFilter] = ()) -> None:
        self.filters = tuple(filters)

    async def evaluate(self, listing: EnrichedListing) -> FilterDecision:
        reasons: list[RejectionReason] = []
        for rule in self.filters:
            reason = await rule.evaluate(listing)
            if reason is not None:
                reasons.append(reason)
        frozen_reasons = tuple(reasons)
        return FilterDecision(accepted=not frozen_reasons, reasons=frozen_reasons)


Predicate = Callable[[EnrichedListing], bool | Awaitable[bool]]


@dataclass(slots=True)
class PredicateFilter:
    name: str
    predicate: Predicate
    code: str
    message: str
    source: ReasonSource = ReasonSource.MACHINE

    async def evaluate(self, listing: EnrichedListing) -> RejectionReason | None:
        result = self.predicate(listing)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        if result:
            return None
        return RejectionReason(
            code=self.code, message=self.message, source=self.source, rule=self.name
        )


@dataclass(slots=True)
class NumericRangeFilter:
    """Rejects missing/non-numeric fields as well as values outside the range."""

    field: str
    minimum: float | None = None
    maximum: float | None = None
    name: str = "numeric_range"

    async def evaluate(self, listing: EnrichedListing) -> RejectionReason | None:
        value: Any = listing.data.get(self.field)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self._reason("missing_or_invalid", value)
        if self.minimum is not None and number < self.minimum:
            return self._reason("below_minimum", number)
        if self.maximum is not None and number > self.maximum:
            return self._reason("above_maximum", number)
        return None

    def _reason(self, suffix: str, value: Any) -> RejectionReason:
        return RejectionReason(
            code=f"{self.field}.{suffix}",
            message=f"{self.field} is outside the accepted range",
            rule=self.name,
            details={
                "field": self.field,
                "value": value,
                "minimum": self.minimum,
                "maximum": self.maximum,
            },
        )
