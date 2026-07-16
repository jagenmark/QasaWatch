from pathlib import Path

import pytest

from qasawatch.parser import latest_home_search_page, parse_qasa_html
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
    assert (second.rent, second.latitude, second.attributes["housing_type"]) == (10300, 59.33, "apartment")


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


def test_qasa_home_search_ignores_nested_location_and_upload_ids():
    payload = {
        "data": {
            "homeIndexSearch": {
                "documents": {
                    "nodes": [
                        {
                            "__typename": "HomeDocument",
                            "id": "1413568",
                            "monthlyCost": 9429,
                            "rent": 8900,
                            "roomCount": 1,
                            "squareMeters": 25,
                            "startDate": "2026-07-15T00:00:00+00:00",
                            "endDate": None,
                            "publishedAt": "2026-07-13T07:11:56Z",
                            "homeType": "apartment",
                            "furnished": False,
                            "petsAllowed": True,
                            "location": {
                                "__typename": "HomeDocumentLocationType",
                                "id": "3496272",
                                "route": "Körsbärsvägen",
                                "streetNumber": None,
                                "locality": "Stockholm",
                                "point": {
                                    "__typename": "GeoPoint",
                                    "lat": 59.349161,
                                    "lon": 18.0640409,
                                },
                            },
                            "uploads": [
                                {
                                    "__typename": "HomeDocumentUploadType",
                                    "id": "20776638",
                                    "url": "https://qasa-static.example/photo.jpg",
                                }
                            ],
                        }
                    ]
                }
            }
        }
    }

    page = parse_qasa_html(
        "<html></html>", captured_json=[payload], results_only=True
    )

    assert [item.external_id for item in page.listings] == ["1413568"]
    listing = page.listings[0]
    assert listing.address == "Körsbärsvägen, Stockholm"
    assert (listing.latitude, listing.longitude) == (59.349161, 18.0640409)
    assert listing.rent == 9429
    assert listing.rental_start == "2026-07-15T00:00:00+00:00"
    assert listing.availability == "until_further_notice"
    assert listing.attributes["base_rent"] == 8900
    assert listing.attributes["furnished"] is False
    assert listing.attributes["pets_allowed"] is True


def test_results_mode_ignores_other_qasa_payloads_recommendations_and_dom_links():
    authoritative = {
        "__qasawatch_operation": "HomeSearch",
        "payload": {
            "data": {
                "homeIndexSearch": {
                    "documents": {
                        "nodes": [
                            {
                                "__typename": "SponsoredCard",
                                "id": "sponsored-1",
                                "monthlyCost": 1,
                            },
                            {
                                "__typename": "HomeDocumentLocationType",
                                "id": "location-1",
                                "roomCount": 2,
                            },
                            {
                                "__typename": "HomeDocument",
                                "id": "real-1",
                                "monthlyCost": 9000,
                                "roomCount": 2,
                            }
                        ]
                    }
                }
            },
            "recommendations": [
                {"__typename": "HomeDocument", "id": "recommended-1", "monthlyCost": 1}
            ],
        },
    }
    unrelated = {
        "__qasawatch_operation": "Recommendations",
        "payload": {
            "data": {
                "homes": [
                    {"__typename": "HomeDocument", "id": "recommended-2", "monthlyCost": 1}
                ]
            }
        },
    }

    page = parse_qasa_html(
        '<a href="/home/dom-only">not a result card</a>',
        captured_json=[unrelated, authoritative],
        results_only=True,
    )

    assert [item.external_id for item in page.listings] == ["real-1"]


def test_home_search_pagination_metadata_uses_latest_authoritative_response():
    first = {
        "__qasawatch_operation": "HomeSearch",
        "payload": {
            "data": {
                "homeIndexSearch": {
                    "documents": {
                        "nodes": [
                            {"__typename": "HomeDocument", "id": "one"},
                            {"__typename": "SponsoredCard", "id": "ad"},
                        ],
                        "hasNextPage": True,
                        "pagesCount": 36,
                        "totalCount": 2085,
                    }
                }
            }
        },
    }
    second = {
        "__qasawatch_operation": "HomeSearch",
        "payload": {
            "data": {
                "homeIndexSearch": {
                    "documents": {
                        "nodes": [{"__typename": "HomeDocument", "id": "two"}],
                        "hasNextPage": False,
                        "pagesCount": 2,
                        "totalCount": 60,
                    }
                }
            }
        },
    }

    info = latest_home_search_page([first, second])

    assert info is not None
    assert info.listing_ids == ("two",)
    assert not info.has_next_page
    assert info.pages_count == 2
    assert info.total_count == 60


def test_detail_fallback_targets_page_id_when_recommendations_are_present():
    page = parse_qasa_html(
        """
        <html><head><title>Testgatan 1, Stockholm - Lägenhet | Qasa</title></head>
        <body>
        <a href="/home/1">Requested listing</a>
        <a href="/home/2">Recommended listing</a>
        Lägenhet · 2 rum · 48 m²
        Hyresperiod 2026-08-22 2027-04-30
        Månadskostnad 10 065 kr
        </body></html>
        """,
        base_url="https://qasa.com/se/sv/home/1",
    )

    requested = next(item for item in page.listings if item.external_id == "1")
    recommendation = next(item for item in page.listings if item.external_id == "2")
    assert requested.address == "Testgatan 1, Stockholm"
    assert (requested.rooms, requested.area, requested.rent) == (2.0, 48.0, 10_065)
    assert (requested.rental_start, requested.rental_end) == (
        "2026-08-22",
        "2027-04-30",
    )
    assert recommendation.rent is None


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
    ("label", "expected"),
    [
        ("Möblerat", True),
        ("Furnished", True),
        ("Omöblerat", False),
        ("Unfurnished", False),
    ],
)
def test_detail_semantic_fallback_distinguishes_furnished_status(label, expected):
    page = parse_qasa_html(
        f'<a href="/home/1">Annons</a> Bostaden hyrs ut {label}',
        base_url="https://qasa.com/se/sv/home/1",
    )

    assert page.listings[0].attributes["furnished"] is expected


def test_detail_semantic_fallback_extracts_open_period_and_move_in_date():
    page = parse_qasa_html(
        """
        <a href="/home/1">Annons</a>
        Hyresperiod Tillsvidare
        Inflyttningsdatum 2026-08-22
        """,
        base_url="https://qasa.com/se/sv/home/1",
    )
    listing = page.listings[0]

    assert listing.availability == "until_further_notice"
    assert listing.rental_start == "2026-08-22"
    assert listing.rental_end is None


@pytest.mark.parametrize("separator", ["-", "–", "—"])
def test_detail_semantic_fallback_accepts_period_dash_separators(separator):
    page = parse_qasa_html(
        f"""
        <a href="/home/1">Annons</a>
        Hyresperiod 2026-08-22 {separator} 2027-04-30
        """,
        base_url="https://qasa.com/se/sv/home/1",
    )
    listing = page.listings[0]

    assert listing.rental_start == "2026-08-22"
    assert listing.rental_end == "2027-04-30"


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
