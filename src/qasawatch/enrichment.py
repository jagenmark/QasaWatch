"""Geocoding and commute enrichment providers.

Google Maps results are subject to Google's storage/caching terms.  The cache
hook here is deliberately opt-in; callers must configure a TTL and storage
policy compatible with their agreement.  Listing-owned coordinates and
non-Google derived status values can normally be retained safely.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .domain import EnrichedListing, RawListing

STOCKHOLM = ZoneInfo("Europe/Stockholm")


@dataclass(frozen=True, slots=True)
class Coordinates:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError("invalid latitude/longitude")


class GeocodeStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    API_FAILURE = "api_failure"


class RouteStatus(StrEnum):
    OK = "ok"
    NO_ROUTE = "no_route"
    API_FAILURE = "api_failure"


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    status: GeocodeStatus
    coordinates: Coordinates | None = None
    formatted_address: str | None = None
    candidates: int = 0
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class RouteResult:
    status: RouteStatus
    duration_seconds: int | None = None
    distance_meters: int | None = None
    diagnostic: str | None = None


class Geocoder(Protocol):
    async def geocode(self, address: str) -> GeocodeResult: ...


class RouteMatrix(Protocol):
    async def compute_route(
        self,
        origin: Coordinates,
        destination: Coordinates,
        *,
        travel_mode: str,
        departure_time: datetime | None = None,
        arrival_time: datetime | None = None,
    ) -> RouteResult: ...


class JsonTransport(Protocol):
    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str], json: Any | None = None
    ) -> tuple[int, Any]: ...


class UrllibJsonTransport:
    """Dependency-free async facade over urllib, mainly for production wiring.

    Inject a fake ``JsonTransport`` in tests. For GET requests, ``json`` is
    encoded as query parameters; for other methods it is a JSON request body.
    """

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def request(self, method: str, url: str, *, headers: Mapping[str, str], json: Any | None = None) -> tuple[int, Any]:
        return await asyncio.to_thread(self._request, method, url, dict(headers), json)

    def _request(self, method: str, url: str, headers: dict[str, str], payload: Any | None) -> tuple[int, Any]:
        body = None
        if method.upper() == "GET" and payload:
            url += ("&" if "?" in url else "?") + urlencode(payload)
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw, status = response.read(), response.status
        except HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            parsed = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        return status, parsed


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    initial_backoff: float = 0.1
    maximum_backoff: float = 1.0

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.initial_backoff < 0 or self.maximum_backoff < 0:
            raise ValueError("invalid retry policy")


class QuotaExceeded(RuntimeError):
    pass


class RequestQuota:
    """A simple per-provider process quota; durable quotas can wrap the transport."""

    def __init__(self, maximum: int | None = None) -> None:
        if maximum is not None and maximum < 0:
            raise ValueError("quota must be non-negative")
        self.maximum = maximum
        self.used = 0

    def consume(self) -> None:
        if self.maximum is not None and self.used >= self.maximum:
            raise QuotaExceeded("provider request quota exceeded")
        self.used += 1


async def _request_with_retry(
    operation: Callable[[], Awaitable[tuple[int, Any]]],
    retry: RetryPolicy,
    quota: RequestQuota,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[int, Any]:
    for attempt in range(retry.attempts):
        quota.consume()
        try:
            status, payload = await operation()
        except Exception:
            if attempt + 1 == retry.attempts:
                raise
        else:
            if status not in {429, 500, 502, 503, 504} or attempt + 1 == retry.attempts:
                return status, payload
        delay = min(retry.initial_backoff * (2**attempt), retry.maximum_backoff)
        await sleep(delay)
    raise AssertionError("unreachable")


class GoogleGeocoder:
    name = "google-geocoding"

    def __init__(self, api_key: str, transport: JsonTransport | None = None, *, retry: RetryPolicy = RetryPolicy(), quota: RequestQuota | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not api_key:
            raise ValueError("Google API key is required")
        self._api_key = api_key
        self._transport = transport or UrllibJsonTransport()
        self._retry, self._quota, self._sleep = retry, quota or RequestQuota(), sleep

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=<redacted>)"

    async def geocode(self, address: str) -> GeocodeResult:
        if not address.strip():
            return GeocodeResult(GeocodeStatus.NOT_FOUND, diagnostic="empty address")
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        async def call() -> tuple[int, Any]:
            return await self._transport.request("GET", url, headers={}, json={"address": address, "key": self._api_key})
        try:
            http, body = await _request_with_retry(call, self._retry, self._quota, self._sleep)
        except Exception as exc:
            return GeocodeResult(GeocodeStatus.API_FAILURE, diagnostic=type(exc).__name__)
        if http != 200 or not isinstance(body, Mapping) or body.get("status") not in {"OK", "ZERO_RESULTS"}:
            return GeocodeResult(GeocodeStatus.API_FAILURE, diagnostic=f"HTTP/status {http}/{body.get('status') if isinstance(body, Mapping) else 'invalid'}")
        results = body.get("results", [])
        if not results:
            return GeocodeResult(GeocodeStatus.NOT_FOUND)
        if len(results) != 1 or results[0].get("partial_match"):
            return GeocodeResult(GeocodeStatus.AMBIGUOUS, candidates=len(results))
        item = results[0]
        location = item.get("geometry", {}).get("location", {})
        try:
            coords = Coordinates(float(location["lat"]), float(location["lng"]))
        except (KeyError, TypeError, ValueError):
            return GeocodeResult(GeocodeStatus.API_FAILURE, diagnostic="malformed geocode response")
        return GeocodeResult(GeocodeStatus.OK, coords, item.get("formatted_address"), 1)


class GoogleRoutesMatrix:
    name = "google-routes-matrix"

    def __init__(self, api_key: str, transport: JsonTransport | None = None, *, retry: RetryPolicy = RetryPolicy(), quota: RequestQuota | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not api_key:
            raise ValueError("Google API key is required")
        self._api_key, self._transport = api_key, transport or UrllibJsonTransport()
        self._retry, self._quota, self._sleep = retry, quota or RequestQuota(), sleep

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=<redacted>)"

    async def compute_route(self, origin: Coordinates, destination: Coordinates, *, travel_mode: str, departure_time: datetime | None = None, arrival_time: datetime | None = None) -> RouteResult:
        if (departure_time is None) == (arrival_time is None):
            raise ValueError("exactly one of departure_time or arrival_time is required")
        mode = travel_mode.upper()
        payload: dict[str, Any] = {
            "origins": [{"waypoint": {"location": {"latLng": {"latitude": origin.latitude, "longitude": origin.longitude}}}}],
            "destinations": [{"waypoint": {"location": {"latLng": {"latitude": destination.latitude, "longitude": destination.longitude}}}}],
            "travelMode": mode,
        }
        selected = departure_time or arrival_time
        assert selected is not None
        payload["departureTime" if departure_time else "arrivalTime"] = selected.isoformat()
        headers = {"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,duration,distanceMeters"}
        async def call() -> tuple[int, Any]:
            return await self._transport.request("POST", "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", headers=headers, json=payload)
        try:
            http, body = await _request_with_retry(call, self._retry, self._quota, self._sleep)
        except Exception as exc:
            return RouteResult(RouteStatus.API_FAILURE, diagnostic=type(exc).__name__)
        if http != 200 or not isinstance(body, list):
            return RouteResult(RouteStatus.API_FAILURE, diagnostic=f"HTTP {http}")
        if not body:
            return RouteResult(RouteStatus.NO_ROUTE)
        row = body[0]
        if row.get("condition") != "ROUTE_EXISTS":
            return RouteResult(RouteStatus.NO_ROUTE, diagnostic=row.get("condition"))
        if row.get("status") and row["status"].get("code", 0) != 0:
            return RouteResult(RouteStatus.API_FAILURE, diagnostic="route element error")
        duration = row.get("duration")
        try:
            seconds = int(math.ceil(float(str(duration).removesuffix("s"))))
        except (TypeError, ValueError):
            return RouteResult(RouteStatus.API_FAILURE, diagnostic="malformed route duration")
        return RouteResult(RouteStatus.OK, seconds, row.get("distanceMeters"))


def next_weekday_at_0800(now: datetime | None = None) -> datetime:
    """Next applicable weekday at 08:00 in Europe/Stockholm (strictly future)."""

    local = (now or datetime.now(STOCKHOLM)).astimezone(STOCKHOLM)
    candidate = datetime.combine(local.date(), time(8), STOCKHOLM)
    if candidate <= local:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def listing_coordinates(data: Mapping[str, Any]) -> Coordinates | None:
    """Read common Qasa coordinate shapes, preferring listing-owned coordinates."""

    candidates = [data, data.get("coordinates"), data.get("location")]
    for value in candidates:
        if not isinstance(value, Mapping):
            continue
        lat = value.get("latitude", value.get("lat"))
        lon = value.get("longitude", value.get("lng", value.get("lon")))
        if lat is not None and lon is not None:
            try:
                return Coordinates(float(lat), float(lon))
            except (TypeError, ValueError):
                pass
    return None


class CommuteEnricher:
    name = "commute"

    def __init__(self, geocoder: Geocoder, routes: RouteMatrix, destination: Coordinates | str | Mapping[str, Coordinates | str], *, travel_mode: str = "TRANSIT", time_kind: str = "arrival", clock: Callable[[], datetime] | None = None) -> None:
        if time_kind not in {"departure", "arrival"}:
            raise ValueError("time_kind must be departure or arrival")
        self.geocoder, self.routes, self.destination = geocoder, routes, destination
        self.travel_mode, self.time_kind = travel_mode, time_kind
        self.clock = clock or (lambda: datetime.now(STOCKHOLM))
        self._resolved_destinations: dict[str, Coordinates] = {}

    async def enrich(self, listing: RawListing) -> EnrichedListing:
        data = dict(listing.data)
        origin = listing_coordinates(data)
        geocode: GeocodeResult | None = None
        if origin is None:
            address = str(data.get("address") or data.get("street_address") or "")
            geocode = await self.geocoder.geocode(address)
            origin = geocode.coordinates
            data["geocode"] = {
                "status": geocode.status.value,
                "formatted_address": geocode.formatted_address,
                "candidates": geocode.candidates,
                "diagnostic": geocode.diagnostic,
            }
            if origin is not None:
                data["latitude"] = origin.latitude
                data["longitude"] = origin.longitude
        if origin is None:
            status = geocode.status.value if geocode else GeocodeStatus.NOT_FOUND.value
            data["commute"] = {"status": status}
            return EnrichedListing(listing.provider, listing.url, listing.external_id, data)
        when = next_weekday_at_0800(self.clock())
        kwargs = {f"{self.time_kind}_time": when}
        configured = self.destination if isinstance(self.destination, Mapping) else {"destination": self.destination}
        commutes: dict[str, Any] = {}
        for name, destination in configured.items():
            if isinstance(destination, str):
                # Fixed destinations are safe to reuse within this process. Do
                # not permanently retain arbitrary Google response payloads.
                resolved = self._resolved_destinations.get(destination)
                result = None
                if resolved is None:
                    result = await self.geocoder.geocode(destination)
                    if result.status is GeocodeStatus.OK and result.coordinates is not None:
                        resolved = result.coordinates
                        self._resolved_destinations[destination] = resolved
                if resolved is None:
                    assert result is not None
                    commutes[str(name)] = {"status": result.status.value, "diagnostic": result.diagnostic}
                    continue
                destination = resolved
            try:
                route = await self.routes.compute_route(origin, destination, travel_mode=self.travel_mode, **kwargs)
            except Exception as exc:
                route = RouteResult(RouteStatus.API_FAILURE, diagnostic=type(exc).__name__)
            commutes[str(name)] = {"status": route.status.value, "duration_seconds": route.duration_seconds, "distance_meters": route.distance_meters, "travel_mode": self.travel_mode, "time_kind": self.time_kind, "at": when.isoformat(), "diagnostic": route.diagnostic}
        data["commutes"] = commutes
        # Preserve the original single-destination contract.
        if len(commutes) == 1:
            data["commute"] = next(iter(commutes.values()))
        return EnrichedListing(listing.provider, listing.url, listing.external_id, data)
