from pathlib import Path

import pytest

from qasawatch.parser import parse_qasa_html
from qasawatch.readiness import PageSample, ReadinessState, classify_samples

FIXTURES = Path(__file__).parent / "fixtures" / "qasa"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")


def test_hierarchical_extraction_deduplicates_and_ignores_css_classes():
    parsed = parse_qasa_html(fixture("results.html"))
    assert [item.external_id for item in parsed.listings] == ["101", "102"]
    first, second = parsed.listings
    assert (first.address, first.rent, first.rooms, first.area) == ("Sveavägen 1, Stockholm", 9800, 2.0, 31.0)
    assert first.provenance["rent"] == "json-ld"
    assert (second.rent, second.latitude, second.attributes["housingType"]) == (10300, 59.33, "apartment")


def test_detail_and_domain_adapter_preserve_provenance():
    listing = parse_qasa_html(fixture("detail.html")).listings[0]
    raw = listing.to_raw_listing()
    assert raw.external_id == "103"
    assert raw.data["furnished"] is True
    assert raw.data["provenance"]["address"] == "json-ld"


def test_malformed_json_is_partial_not_fatal_and_dom_fallback_works():
    parsed = parse_qasa_html(fixture("partial.html"))
    assert len(parsed.listings) == 1
    assert parsed.listings[0].rent == 7500
    assert parsed.listings[0].provenance["rent"] == "accessibility"
    assert parsed.errors and "invalid next-data" in parsed.errors[0]


def test_first_party_captured_json_is_an_extraction_source():
    payload = {"data": {"homes": [{"listingId": "105", "monthlyRent": 9100, "roomCount": 2}]}}
    listing = parse_qasa_html("<html></html>", captured_json=[payload]).listings[0]
    assert (listing.external_id, listing.rent) == ("105", 9100)
    assert listing.provenance["rent"] == "captured-json"


def test_generic_json_ids_are_not_misclassified_as_listings():
    html = '<script type="application/json">{"user":{"id":"not-a-home","name":"Alice"}}</script>'
    assert parse_qasa_html(html).listings == ()


def test_captured_asset_with_numeric_id_is_not_a_listing():
    page = parse_qasa_html(
        '<a href="/home/1413701" aria-label="Bäckvägen, 2 rum, 48 m²"></a>',
        captured_json=[
            {
                "id": "20779510",
                "url": "https://qasa-static-prod.s3-eu-west-1.amazonaws.com/img/photo.jpg",
            },
            {
                "id": "1413701",
                "url": "https://qasa.com/home/1413701",
                "monthlyCost": 9500,
            },
        ],
    )

    assert [item.external_id for item in page.listings] == ["1413701"]
    assert page.listings[0].url == "https://qasa.com/home/1413701"


def test_detail_semantic_fallback_extracts_period_total_rent_and_duration():
    page = parse_qasa_html(
        """
        <html><head><title>Bäckvägen, Hägersten - Lägenhet | Qasa</title></head>
        <body><a href="/home/1413701">Annons</a>
        Lägenhet · 2 rum · 48 m²
        Hyresperiod 2026-08-22 2027-04-30 Möjlighet till förlängning
        Hyra Månadskostnad 10 065 kr Hyra 9 500 kr Serviceavgift 565 kr
        </body></html>
        """,
    )
    listing = page.listings[0]
    assert listing.address == "Bäckvägen, Hägersten"
    assert listing.rent == 10_065
    assert listing.attributes["base_rent"] == 9_500
    assert listing.duration == "251 days"
    assert listing.availability == "extension_possible"


@pytest.mark.parametrize(
    ("name", "attribute"),
    [("empty.html", "explicit_empty"), ("incomplete.html", "loading"), ("auth.html", "auth_required")],
)
def test_page_signals(name, attribute):
    assert getattr(parse_qasa_html(fixture(name)), attribute)


def test_empty_requires_explicit_stable_evidence():
    no_evidence = [PageSample("https://qasa.com/find"), PageSample("https://qasa.com/find")]
    assert classify_samples(no_evidence).state == ReadinessState.INCOMPLETE
    explicit = [PageSample("https://qasa.com/find", explicit_empty=True)] * 2
    assert classify_samples(explicit).state == ReadinessState.EMPTY


def test_results_must_stabilize_and_blocking_signals_win():
    changing = [PageSample("https://qasa.com", ("1",)), PageSample("https://qasa.com", ("1", "2"))]
    assert classify_samples(changing).state == ReadinessState.INCOMPLETE
    ready = changing + [PageSample("https://qasa.com", ("1", "2"))]
    assert classify_samples(ready).state == ReadinessState.READY
    assert classify_samples([PageSample("https://qasa.com", captcha=True)]).state == ReadinessState.CAPTCHA
