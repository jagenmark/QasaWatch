"""Replaceable GeoJSON adapter for SCB demographic datasets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .domain import EnrichedListing, RawListing
from .enrichment import Coordinates, listing_coordinates


class DatasetError(ValueError):
    """Dataset is present but cannot be used safely."""


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    source: str
    vintage: str
    crs: str = "EPSG:4326"


@dataclass(frozen=True, slots=True)
class FieldMapping:
    id_field: str = "id"
    name_field: str = "name"
    demographic_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AreaMatch:
    matched: bool
    area_id: str | None = None
    area_name: str | None = None
    demographics: Mapping[str, Any] = field(default_factory=dict)
    metadata: DatasetMetadata | None = None
    diagnostic: str | None = None


class DemographicDataset(Protocol):
    async def lookup(self, point: Coordinates) -> AreaMatch: ...


def _ring_contains(ring: list[list[float]], x: float, y: float) -> bool:
    if len(ring) < 4:
        raise DatasetError("polygon ring must contain at least four coordinates")
    inside = False
    j = len(ring) - 1
    for i, current in enumerate(ring):
        previous = ring[j]
        try:
            xi, yi = float(current[0]), float(current[1])
            xj, yj = float(previous[0]), float(previous[1])
        except (IndexError, TypeError, ValueError) as exc:
            raise DatasetError("malformed polygon coordinate") from exc
        # Boundary counts as contained.
        cross = (x - xi) * (yj - yi) - (y - yi) * (xj - xi)
        if abs(cross) < 1e-10 and min(xi, xj) <= x <= max(xi, xj) and min(yi, yj) <= y <= max(yi, yj):
            return True
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _polygon_contains(polygon: list[Any], x: float, y: float) -> bool:
    if not polygon or not _ring_contains(polygon[0], x, y):
        return False
    return not any(_ring_contains(hole, x, y) for hole in polygon[1:])


class GeoJSONSCBDataset:
    """In-memory WGS84 GeoJSON FeatureCollection adapter.

    Other CRS or source formats should be converted by another implementation;
    silently treating projected coordinates as longitude/latitude is forbidden.
    """

    def __init__(self, document: Mapping[str, Any], *, fields: FieldMapping = FieldMapping(), expected_source: str | None = None, expected_vintage: str | None = None) -> None:
        if document.get("type") != "FeatureCollection":
            raise DatasetError("SCB dataset must be a GeoJSON FeatureCollection")
        meta = document.get("metadata")
        if not isinstance(meta, Mapping):
            raise DatasetError("SCB dataset metadata is missing")
        source, vintage = str(meta.get("source", "")).strip(), str(meta.get("vintage", "")).strip()
        crs = str(meta.get("crs", "EPSG:4326")).upper()
        if not source or not vintage:
            raise DatasetError("SCB source and vintage metadata are required")
        if crs not in {"EPSG:4326", "CRS84", "OGC:CRS84"}:
            raise DatasetError(f"unsupported SCB dataset CRS: {crs}")
        if expected_source is not None and source != expected_source:
            raise DatasetError(f"unexpected SCB dataset source: {source}")
        if expected_vintage is not None and vintage != expected_vintage:
            raise DatasetError(f"unexpected SCB dataset vintage: {vintage}")
        features = document.get("features")
        if not isinstance(features, list):
            raise DatasetError("SCB features must be a list")
        self.metadata = DatasetMetadata(source, vintage, crs)
        self.fields, self.features = fields, features
        self._validate()

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> "GeoJSONSCBDataset":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetError(f"unable to read SCB GeoJSON: {type(exc).__name__}") from exc
        if not isinstance(document, Mapping):
            raise DatasetError("SCB GeoJSON root must be an object")
        return cls(document, **kwargs)

    def _validate(self) -> None:
        for index, feature in enumerate(self.features):
            if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
                raise DatasetError(f"feature {index} is malformed")
            props, geometry = feature.get("properties"), feature.get("geometry")
            if not isinstance(props, Mapping) or self.fields.id_field not in props or self.fields.name_field not in props:
                raise DatasetError(f"feature {index} lacks configured id/name fields")
            if not isinstance(geometry, Mapping) or geometry.get("type") not in {"Polygon", "MultiPolygon"} or not isinstance(geometry.get("coordinates"), list):
                raise DatasetError(f"feature {index} has unsupported geometry")

    async def lookup(self, point: Coordinates) -> AreaMatch:
        for index, feature in enumerate(self.features):
            geometry = feature["geometry"]
            polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
            try:
                contained = any(_polygon_contains(polygon, point.longitude, point.latitude) for polygon in polygons)
            except DatasetError as exc:
                return AreaMatch(False, metadata=self.metadata, diagnostic=f"feature {index}: {exc}")
            if contained:
                props = feature["properties"]
                values = {output: props.get(source) for output, source in self.fields.demographic_fields.items()}
                return AreaMatch(True, str(props[self.fields.id_field]), str(props[self.fields.name_field]), values, self.metadata)
        return AreaMatch(False, metadata=self.metadata, diagnostic="point is outside dataset polygons")


class OptionalSCBDataset:
    """Gracefully returns partial enrichment when no local dataset is installed."""

    def __init__(
        self,
        dataset: DemographicDataset | None,
        *,
        diagnostic: str = "SCB dataset unavailable",
    ) -> None:
        self.dataset = dataset
        self.diagnostic = diagnostic

    async def lookup(self, point: Coordinates) -> AreaMatch:
        if self.dataset is None:
            return AreaMatch(False, diagnostic=self.diagnostic)
        return await self.dataset.lookup(point)


class SCBEnricher:
    """Attach a replaceable local demographic match without failing the scan."""

    name = "scb-demographics"

    def __init__(self, dataset: DemographicDataset) -> None:
        self.dataset = dataset

    async def enrich(self, listing: RawListing) -> EnrichedListing:
        data = dict(listing.data)
        coordinates = listing_coordinates(data)
        if coordinates is None:
            data["scb"] = {
                "status": "unavailable",
                "diagnostic": "listing coordinates are unavailable",
            }
            return EnrichedListing(listing.provider, listing.url, listing.external_id, data)
        try:
            match = await self.dataset.lookup(coordinates)
        except Exception as exc:
            data["scb"] = {
                "status": "error",
                "diagnostic": f"SCB lookup failed ({type(exc).__name__})",
            }
            return EnrichedListing(listing.provider, listing.url, listing.external_id, data)
        data["scb"] = {
            "status": "matched" if match.matched else "not_matched",
            "area_id": match.area_id,
            "area_name": match.area_name,
            "source": match.metadata.source if match.metadata else None,
            "vintage": match.metadata.vintage if match.metadata else None,
            "crs": match.metadata.crs if match.metadata else None,
            "diagnostic": match.diagnostic,
        }
        data["demographics"] = dict(match.demographics)
        return EnrichedListing(listing.provider, listing.url, listing.external_id, data)

# Compatibility-friendly concise name.
SCBDataset = GeoJSONSCBDataset
