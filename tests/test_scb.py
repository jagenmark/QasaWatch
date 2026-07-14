import pytest

from qasawatch.enrichment import Coordinates
from qasawatch.scb import DatasetError, FieldMapping, GeoJSONSCBDataset, OptionalSCBDataset


def document():
    return {"type": "FeatureCollection", "metadata": {"source": "SCB", "vintage": "2025", "crs": "EPSG:4326"}, "features": [{"type": "Feature", "properties": {"code": "A", "label": "Area", "population": 42}, "geometry": {"type": "Polygon", "coordinates": [[[17, 59], [19, 59], [19, 60], [17, 60], [17, 59]]]}}]}


@pytest.mark.asyncio
async def test_match_and_mapping():
    ds = GeoJSONSCBDataset(document(), fields=FieldMapping("code", "label", {"population": "population"}))
    result = await ds.lookup(Coordinates(59.5, 18))
    assert result.matched and result.area_id == "A" and result.demographics["population"] == 42


def test_metadata_failure():
    value = document(); value["metadata"]["crs"] = "EPSG:3006"
    with pytest.raises(DatasetError, match="CRS"):
        GeoJSONSCBDataset(value)


@pytest.mark.asyncio
async def test_no_dataset_is_partial_result():
    result = await OptionalSCBDataset(None).lookup(Coordinates(59.5, 18))
    assert not result.matched and "unavailable" in result.diagnostic
