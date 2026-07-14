"""Deterministic page-readiness classification without ``networkidle``.

Qasa pages keep background connections open, so readiness is based on repeated
semantic samples.  Only an explicit empty-state marker can produce EMPTY.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class ReadinessState(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    INCOMPLETE = "incomplete"
    AUTH_REQUIRED = "auth_required"
    CAPTCHA = "captcha"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PageSample:
    url: str
    listing_keys: tuple[str, ...] = ()
    explicit_empty: bool = False
    loading: bool = False
    auth_required: bool = False
    captcha: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    state: ReadinessState
    reason: str
    listing_keys: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.state in (ReadinessState.READY, ReadinessState.EMPTY)


def classify_samples(samples: Iterable[PageSample], *, stable_samples: int = 2) -> ReadinessResult:
    """Classify consecutive observations, requiring stable semantic content."""
    observed = list(samples)
    if not observed:
        return ReadinessResult(ReadinessState.INCOMPLETE, "no page samples")
    last = observed[-1]
    if last.captcha:
        return ReadinessResult(ReadinessState.CAPTCHA, "CAPTCHA or bot challenge detected")
    if last.auth_required:
        return ReadinessResult(ReadinessState.AUTH_REQUIRED, "login or authentication required")
    if last.error:
        return ReadinessResult(ReadinessState.ERROR, last.error)
    required = max(1, stable_samples)
    tail = observed[-required:]
    if len(tail) < required:
        return ReadinessResult(ReadinessState.INCOMPLETE, "not enough stable samples")
    if any(item.loading or item.captcha or item.auth_required or item.error for item in tail):
        return ReadinessResult(ReadinessState.INCOMPLETE, "page is still loading or blocked")
    signatures = [(item.listing_keys, item.explicit_empty) for item in tail]
    if len(set(signatures)) != 1:
        return ReadinessResult(ReadinessState.INCOMPLETE, "results have not stabilized")
    if last.listing_keys:
        return ReadinessResult(ReadinessState.READY, "stable listing results", last.listing_keys)
    if last.explicit_empty:
        return ReadinessResult(ReadinessState.EMPTY, "stable explicit empty result")
    return ReadinessResult(
        ReadinessState.INCOMPLETE,
        "no listings and no explicit empty-result evidence",
    )
