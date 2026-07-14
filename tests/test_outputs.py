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
    rich = ListingSnapshot(value.id, value.provider, value.url, value.external_id, value.stage, {**value.data, "rooms": 2, "area": 48, "latitude": 59.3, "longitude": 18.0, "rental_start": "2026-08-22", "rental_end": "2027-04-30", "duration": "251 days", "availability": "available", "published_at": "2026-01-01", "commutes": {"work": {"duration_seconds": 600}}, "demographics": {"population": 42}, "filter_result": {"status": "accepted"}}, value.discovered_at)
    summary = listing_summary(rich)
    assert summary["commute"] == "work: 10 min" and "population: 42" in summary["demographics"] and summary["filter"] == "accepted"
    assert summary["coordinates"] == "59.3, 18.0"
    assert summary["rental_period"] == "2026-08-22 → 2027-04-30"
