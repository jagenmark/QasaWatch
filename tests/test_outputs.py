from datetime import UTC, datetime

import pytest

from qasawatch.domain import DeliveryChannel, ListingSnapshot, ListingStage
from qasawatch.outputs import DiscordWebhookOutput, GoogleSheetsOutput, deliver_independently, listing_summary, output_idempotency_key


def listing(id=7):
    return ListingSnapshot(id, "qasa", "https://listing", "x", ListingStage.ACCEPTED, {"title": "Home", "rent": 100}, datetime.now(UTC))


class Sheets:
    async def contains_idempotency_key(self, *args): return False
    async def append_row(self, *args): return "range"


class BadWebhook:
    async def post(self, *args, **kwargs): raise OSError("secret response")


class Webhook:
    def __init__(self):
        self.payload = None

    async def post(self, url, payload, *, headers=None):
        self.payload = payload
        return {"id": "message-1"}


@pytest.mark.asyncio
async def test_sheets_success_discord_failure_are_independent():
    results = await deliver_independently([GoogleSheetsOutput(Sheets(), "sheet"), DiscordWebhookOutput("https://discord.test/hook", BadWebhook())], listing())
    assert results[0].result is not None
    assert results[1].error is not None


def test_key_stable():
    assert output_idempotency_key(listing(), DeliveryChannel.EMAIL) == output_idempotency_key(listing(), "email")
    assert output_idempotency_key(listing(), "email") != output_idempotency_key(listing(8), "email")


def test_rich_summary():
    value = listing()
    rich = ListingSnapshot(value.id, value.provider, value.url, value.external_id, value.stage, {**value.data, "rooms": 2.0, "area": 48.0, "latitude": 59.3, "longitude": 18.0, "rental_start": "2026-08-22T00:00:00+00:00", "rental_end": "2027-04-30T00:00:00+00:00", "duration": "251 days", "availability": "available", "published_at": "2026-01-01", "commutes": {"work": {"duration_seconds": 600}}, "demographics": {"population": 42}, "filter_result": {"status": "accepted"}}, value.discovered_at)
    summary = listing_summary(rich)
    assert summary["commute"] == "work: 10 min" and "population: 42" in summary["demographics"] and summary["filter"] == "accepted"
    assert summary["coordinates"] == "59.3, 18.0"
    assert summary["rental_period"] == "2026-08-22 → 2027-04-30"
    assert summary["rooms"] == "2" and summary["area"] == "48"


@pytest.mark.asyncio
async def test_discord_formats_swedish_listing_with_separate_enrichment_bullets():
    value = listing()
    rich = ListingSnapshot(
        value.id,
        value.provider,
        value.url,
        value.external_id,
        value.stage,
        {
            **value.data,
            "address": "Sveavägen 1, Stockholm",
            "rooms": 2,
            "area": 48,
            "rental_start": "2026-08-22",
            "rental_end": "2027-04-30",
            "commutes": {
                "arbete": {"duration_seconds": 600},
                "skola": {"status": "api_failure"},
                "förskola": {},
            },
            "demographics": {
                "foreign_background_percent": 46.6,
                "population": 1846,
                "area_level": "DeSO",
                "precision": "neighborhood-level estimate, not exact address-level data",
                "source": "SCB",
                "reference_year": "2025",
            },
        },
        value.discovered_at,
    )
    client = Webhook()

    result = await DiscordWebhookOutput("https://discord.test/hook", client).deliver(
        rich, idempotency_key="key"
    )

    assert result.provider_message_id == "message-1"
    assert client.payload == {
        "content": (
            "**NY QASA-ANNONS**\n"
            "https://listing\n\n"
            "Hyra: 100 kr\n"
            "Kvm: 48 m²\n"
            "Plats/adress: Sveavägen 1, Stockholm\n"
            "Rum: 2\n"
            "Uthyrningsperiod: 2026-08-22 → 2027-04-30\n\n"
            "Pendling:\n"
            "- arbete: 10 min\n"
            "- skola: api_failure\n"
            "- förskola: unknown\n\n"
            "Brown Watch / Demographics:\n"
            "- Foreign background in the surrounding area: approx. 46.6%\n"
            "- Population: 1846\n"
            "- Area level: DeSO\n"
            "- Precision: neighborhood-level estimate, not exact address-level data\n"
            "- Source: SCB 2025"
        ),
        "allowed_mentions": {"parse": []},
    }


@pytest.mark.asyncio
async def test_discord_omits_missing_fields_and_handles_open_ended_period_safely():
    value = listing()
    sparse = ListingSnapshot(
        value.id,
        value.provider,
        value.url,
        value.external_id,
        value.stage,
        {
            "rental_start": "2026-08-22",
            "demographics": {"notis": "@here", "saknas": None},
        },
        value.discovered_at,
    )
    client = Webhook()

    await DiscordWebhookOutput("https://discord.test/hook", client).deliver(
        sparse, idempotency_key="key"
    )

    assert "Uthyrningsperiod: 2026-08-22 - Tillsvidare" in client.payload["content"]
    assert "Hyra:" not in client.payload["content"]
    assert "Pendling:" not in client.payload["content"]
    assert "- Notis: @here" in client.payload["content"]
    assert "saknas" not in client.payload["content"]
    assert len(client.payload["content"]) <= 1900
    assert client.payload["allowed_mentions"] == {"parse": []}
