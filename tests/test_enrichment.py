from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from qasawatch.domain import RawListing
from qasawatch.enrichment import (
    CommuteEnricher, Coordinates, GeocodeResult, GeocodeStatus,
    GeocodingEnricher, RouteResult, RouteStatus, next_weekday_at_0800,
)

TZ = ZoneInfo("Europe/Stockholm")


class Geocoder:
    def __init__(self): self.calls = []
    async def geocode(self, address):
        self.calls.append(address)
        return GeocodeResult(GeocodeStatus.OK, Coordinates(59.3, 18.0))


class Routes:
    def __init__(self): self.kwargs = None
    async def compute_route(self, origin, destination, **kwargs):
        self.kwargs = kwargs
        return RouteResult(RouteStatus.OK, 123, 456)


def test_next_weekday_calendar():
    assert next_weekday_at_0800(datetime(2026, 7, 17, 9, tzinfo=TZ)) == datetime(2026, 7, 20, 8, tzinfo=TZ)
    assert next_weekday_at_0800(datetime(2026, 7, 20, 7, tzinfo=TZ)) == datetime(2026, 7, 20, 8, tzinfo=TZ)


@pytest.mark.asyncio
async def test_geocoding_enricher_supports_scb_without_commute_destinations():
    geocoder = Geocoder()
    result = await GeocodingEnricher(geocoder).enrich(
        RawListing(
            "qasa",
            "https://x",
            data={"address": "Sveavägen 1, Stockholm"},
        )
    )

    assert geocoder.calls == ["Sveavägen 1, Stockholm"]
    assert result.data["latitude"] == 59.3
    assert result.data["longitude"] == 18.0
    assert result.data["geocode"]["status"] == "ok"


@pytest.mark.asyncio
async def test_geocoding_enricher_reuses_qasa_coordinates_without_api_call():
    geocoder = Geocoder()
    result = await GeocodingEnricher(geocoder).enrich(
        RawListing(
            "qasa",
            "https://x",
            data={"latitude": 59.2, "longitude": 18.1},
        )
    )

    assert geocoder.calls == []
    assert result.data["latitude"] == 59.2


@pytest.mark.asyncio
async def test_listing_coords_first_and_explicit_arrival_mode():
    geocoder, routes = Geocoder(), Routes()
    enricher = CommuteEnricher(geocoder, routes, Coordinates(59.4, 18.1), travel_mode="BICYCLING", time_kind="arrival", clock=lambda: datetime(2026, 7, 17, 9, tzinfo=TZ))
    result = await enricher.enrich(RawListing("qasa", "https://x", data={"lat": 59.2, "lng": 17.9, "address": "unused"}))
    assert not geocoder.calls
    assert routes.kwargs["travel_mode"] == "BICYCLING"
    assert routes.kwargs["arrival_time"].weekday() == 0
    assert result.data["commute"]["duration_seconds"] == 123


@pytest.mark.asyncio
async def test_named_destinations_geocode_origin_once_and_isolate_routes():
    geocoder = Geocoder()
    class PartialRoutes:
        calls = 0
        async def compute_route(self, origin, destination, **kwargs):
            self.calls += 1
            if self.calls == 2: raise OSError("down")
            return RouteResult(RouteStatus.OK, 60, 100)
    enriched = await CommuteEnricher(geocoder, PartialRoutes(), {"work": Coordinates(59.4, 18.1), "school": Coordinates(59.5, 18.2)}).enrich(RawListing("qasa", "https://x", data={"address": "Home"}))
    assert geocoder.calls == ["Home"]
    assert enriched.data["commutes"]["work"]["status"] == "ok"
    assert enriched.data["commutes"]["school"]["status"] == "api_failure"
