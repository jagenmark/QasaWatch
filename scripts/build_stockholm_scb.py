"""Build the local Stockholm DeSO 2025 demographic dataset from SCB open data."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


WFS_URL = "https://geodata.scb.se/geoserver/stat/ows"
PXWEB_URL = (
    "https://api.scb.se/OV0104/v1/doris/en/ssd/"
    "BE/BE0101/BE0101Y/FolkmDesoBakgrKon"
)
MUNICIPALITY = "0180"
YEAR = "2025"


def _request_json(url: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "QasaWatch-SCB-Dataset-Builder/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("SCB response root is not an object")
    return value


def _geography() -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "stat:DeSO_2025",
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "CQL_FILTER": f"kommunkod='{MUNICIPALITY}'",
        }
    )
    document = _request_json(f"{WFS_URL}?{query}")
    features = document.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("SCB WFS returned no Stockholm DeSO features")
    return document


def _statistics(codes: list[str]) -> dict[str, tuple[int | None, int | None]]:
    regions = [f"{code}_DeSO2025" for code in codes]
    payload = {
        "query": [
            {"code": "Region", "selection": {"filter": "item", "values": regions}},
            {"code": "UtlBakgrund", "selection": {"filter": "item", "values": ["1", "SA"]}},
            {"code": "Kon", "selection": {"filter": "item", "values": ["1+2"]}},
            {"code": "ContentsCode", "selection": {"filter": "item", "values": ["000007Y4"]}},
            {"code": "Tid", "selection": {"filter": "item", "values": [YEAR]}},
        ],
        "response": {"format": "json-stat2"},
    }
    result = _request_json(PXWEB_URL, payload=payload)
    if result.get("class") != "dataset":
        raise ValueError("SCB PxWeb response is not a JSON-stat2 dataset")
    dimensions = result["dimension"]
    sizes = result["size"]
    order = result["id"]
    values = result["value"]

    def position(dimension: str, code: str) -> int:
        index = dimensions[dimension]["category"]["index"]
        if isinstance(index, dict):
            return int(index[code])
        return list(index).index(code)

    def observation(region: str, background: str) -> int | None:
        coordinates = {
            "Region": position("Region", region),
            "UtlBakgrund": position("UtlBakgrund", background),
            "Kon": position("Kon", "1+2"),
            "ContentsCode": position("ContentsCode", "000007Y4"),
            "Tid": position("Tid", YEAR),
        }
        offset = 0
        for dimension, size in zip(order, sizes):
            offset = offset * int(size) + coordinates[dimension]
        raw = values.get(str(offset)) if isinstance(values, dict) else values[offset]
        return int(raw) if raw is not None else None

    return {
        code: (
            observation(f"{code}_DeSO2025", "1"),
            observation(f"{code}_DeSO2025", "SA"),
        )
        for code in codes
    }


def build(output: Path) -> dict[str, Any]:
    geography = _geography()
    features = geography["features"]
    codes = [str(feature["properties"]["desokod"]) for feature in features]
    statistics = _statistics(codes)
    missing = 0
    for feature in features:
        properties = feature["properties"]
        code = str(properties["desokod"])
        foreign, population = statistics[code]
        percentage = (
            round(100 * foreign / population, 1)
            if foreign is not None and population not in (None, 0)
            else None
        )
        missing += int(percentage is None)
        properties.update(
            {
                "deso_id": code,
                "deso_name": f"DeSO {code}",
                "population": population,
                "foreign_background_count": foreign,
                "foreign_background_percent": percentage,
                "area_level": "DeSO",
                "precision": "neighborhood-level statistical area; not exact address-level data",
                "source_label": "SCB",
                "reference_year": YEAR,
            }
        )
    document = {
        "type": "FeatureCollection",
        "metadata": {
            "source": "SCB",
            "vintage": YEAR,
            "crs": "EPSG:4326",
            "region": "Stockholm municipality (0180)",
            "generated_at": datetime.now(UTC).isoformat(),
            "geography_source": WFS_URL,
            "statistics_source": PXWEB_URL,
            "statistics_table": "FolkmDesoBakgrKon / TAB6571",
            "disclosure_control": (
                "SCB applies Cell Key Method uncertainty from reference year 2025; "
                "reported components and totals may not add exactly."
            ),
        },
        "features": features,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return {"features": len(features), "missing_statistics": missing, "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/scb/stockholm-deso-2025.geojson"),
    )
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
