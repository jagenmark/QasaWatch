import asyncio

import pytest
from sqlalchemy import func, select

from qasawatch.browser import BrowserScan
from qasawatch.db import Database
from qasawatch.domain import DeliveryChannel, DeliveryResult, EnrichedListing, RawListing
from qasawatch.emailer import EmailMode, EmailOutput
from qasawatch.filters import FilterChain, NumericRangeFilter
from qasawatch.models import (
    DeliveryAttempt,
    EmailBatch,
    Listing,
    ListingDelivery,
    ManualProcessing,
    ProcessingError,
    Run,
    RunListing,
)
from qasawatch.parser import ParsedListing, ParsedPage
from qasawatch.pipeline import Pipeline
from qasawatch.readiness import ReadinessResult, ReadinessState
from qasawatch.schemas import PromotionRequest, WatcherConfig
from qasawatch.service import AppService, IncompletePageError


class FakeBrowser:
    def __init__(self, rent=9000): self.rent = rent; self.scan_modes = []
    async def scan(self, url, *, results_only=False):
        self.scan_modes.append(results_only)
        item = ParsedListing(url=url, external_id="manual-1", rent=self.rent, address="Test")
        return BrowserScan(ParsedPage((item,)), ReadinessResult(ReadinessState.READY, "ok", ("manual-1",)), url)


class Output:
    channel = DeliveryChannel.DISCORD
    def __init__(self): self.calls = []
    async def deliver(self, listing, *, idempotency_key):
        self.calls.append(listing.id); return DeliveryResult("sent")


class FailingOutput(Output):
    async def deliver(self, listing, *, idempotency_key):
        self.calls.append(listing.id)
        raise RuntimeError("Discord unavailable")


class CapturingOutput(Output):
    async def deliver(self, listing, *, idempotency_key):
        self.calls.append(listing)
        return DeliveryResult("sent")


class MailSender:
    def __init__(self): self.calls = []
    async def send(self, recipients, subject, body):
        self.calls.append((tuple(recipients), subject, body))
        return "mail-1"


class CountingEnricher:
    name = "counting"

    def __init__(self):
        self.calls = 0

    async def enrich(self, raw):
        self.calls += 1
        return EnrichedListing(
            raw.provider,
            raw.url,
            raw.external_id,
            {**raw.data, "coordinates": {"latitude": 59.3, "longitude": 18.0}},
        )


class FailingEnricher:
    name = "failing"

    async def enrich(self, raw):
        raise RuntimeError("bad maps key")


@pytest.fixture
async def database(tmp_path):
    db = Database(tmp_path / "state.db"); await db.initialize(); yield db; await db.dispose()


async def test_manual_rejection_is_full_and_does_not_create_watcher_history(database):
    browser = FakeBrowser(20_000)
    service = AppService(database, browser, Pipeline(database, filters=FilterChain([NumericRangeFilter("rent", maximum=10_000)])))
    history_id, result = await service.process_manual("https://qasa.com/se/sv/home/manual-1")
    assert history_id and not result.decision.accepted and result.data["rent"] == 20_000
    assert browser.scan_modes == [False]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(ManualProcessing.id))) == 1
        assert await session.scalar(select(func.count(Listing.id))) == 0
        assert await session.scalar(select(func.count(Run.id))) == 0


async def test_manual_listing_that_passes_filters_is_fully_returned(database):
    service = AppService(
        database,
        FakeBrowser(9_000),
        Pipeline(
            database,
            filters=FilterChain([NumericRangeFilter("rent", maximum=10_000)]),
        ),
    )
    _, result = await service.process_manual("https://qasa.com/home/manual-1")
    assert result.decision.accepted
    assert result.data["address"] == "Test"
    assert result.data["rent"] == 9_000
    assert result.listing_id is None


async def test_manual_promotion_only_delivers_after_explicit_review(database):
    output = Output(); service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(WatcherConfig(safe_mode=False))
    history_id, _ = await service.process_manual("https://qasa.com/se/sv/home/manual-1")
    assert output.calls == []
    promoted = await service.promote_manual(PromotionRequest(manual_id=history_id, channels=["discord"]))
    assert promoted.listing_id is not None and len(output.calls) == 1
    assert promoted.delivery_statuses["discord"]["outcome"] == "sent"
    async with database.sessions() as session:
        promotion = await session.scalar(
            select(ManualProcessing).where(ManualProcessing.action == "promote")
        )
        assert promotion.status == "succeeded"
        assert promotion.result["delivery_statuses"]["discord"]["outcome"] == "sent"


async def test_repeated_manual_promotion_sends_again_with_a_fresh_attempt(
    database,
):
    output = Output()
    service = AppService(
        database, FakeBrowser(), Pipeline(database, outputs=[output])
    )
    await service.save_config(WatcherConfig(safe_mode=False))
    history_id, _ = await service.process_manual(
        "https://qasa.com/se/sv/home/manual-1"
    )

    first = await service.promote_manual(
        PromotionRequest(manual_id=history_id, channels=["discord"])
    )
    second = await service.promote_manual(
        PromotionRequest(manual_id=history_id, channels=["discord"])
    )

    assert first.delivery_statuses["discord"]["outcome"] == "sent"
    assert second.delivery_statuses["discord"]["outcome"] == "sent"
    assert len(output.calls) == 2
    async with database.sessions() as session:
        promotions = (
            await session.scalars(
                select(ManualProcessing)
                .where(ManualProcessing.action == "promote")
                .order_by(ManualProcessing.id)
            )
        ).all()
        assert len(promotions) == 2
        assert promotions[-1].result["delivery_statuses"]["discord"]["outcome"] == "sent"
        delivery = await session.scalar(
            select(ListingDelivery).where(ListingDelivery.channel == "discord")
        )
        attempts = (
            await session.scalars(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery.id)
                .order_by(DeliveryAttempt.sequence)
            )
        ).all()
        assert [attempt.sequence for attempt in attempts] == [1, 2]
        assert attempts[0].idempotency_key != attempts[1].idempotency_key
        assert attempts[1].idempotency_key.endswith(":manual:2")


async def test_manual_promotion_refreshes_stale_duplicate_with_reviewed_enrichment(database):
    await Pipeline(database).process(
        RawListing(
            "qasa",
            "https://qasa.com/home/manual-1",
            "manual-1",
            {
                "rent": 9000,
                "address": "Old watcher data",
                "rental_start": "2026-07-01",
                "availability": "until_further_notice",
            },
        )
    )

    class RichEnricher:
        name = "rich-manual"

        async def enrich(self, raw):
            return EnrichedListing(
                raw.provider,
                raw.url,
                raw.external_id,
                {
                    **raw.data,
                    "address": "Reviewed address",
                    "commutes": {
                        "T-Centralen": {"status": "ok", "duration_seconds": 1200}
                    },
                    "demographics": {
                        "area_level": "DeSO",
                        "foreign_background_percent": 22.9,
                    },
                },
            )

    output = CapturingOutput()
    service = AppService(
        database,
        FakeBrowser(),
        Pipeline(database, enricher=RichEnricher(), outputs=[output]),
    )
    await service.save_config(WatcherConfig(safe_mode=False))
    history_id, reviewed = await service.process_manual(
        "https://qasa.com/home/manual-1"
    )

    promoted = await service.promote_manual(
        PromotionRequest(manual_id=history_id, channels=["discord"])
    )

    assert reviewed.data["commutes"]["T-Centralen"]["duration_seconds"] == 1200
    assert promoted.duplicate is True
    assert len(output.calls) == 1
    sent = output.calls[0]
    assert sent.data["address"] == "Reviewed address"
    assert sent.data["rental_start"] == "2026-07-01"
    assert sent.data["availability"] == "until_further_notice"
    assert sent.data["commutes"]["T-Centralen"]["duration_seconds"] == 1200
    assert sent.data["demographics"]["foreign_background_percent"] == 22.9
    async with database.sessions() as session:
        durable = await session.get(Listing, promoted.listing_id)
        assert durable.data["commutes"] == sent.data["commutes"]
        assert durable.data["demographics"] == sent.data["demographics"]


async def test_manual_email_promotion_sends_one_message_in_grouped_scan_mode(database):
    sender = MailSender()
    grouped = EmailOutput(
        sender,
        ["reviewer@example.test"],
        mode=EmailMode.PER_SCAN,
    )
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[grouped]))
    await service.save_config(WatcherConfig(safe_mode=False))
    history_id, _ = await service.process_manual("https://qasa.com/home/manual-1")

    promoted = await service.promote_manual(
        PromotionRequest(manual_id=history_id, channels=["email"])
    )

    assert promoted.listing_id is not None
    assert len(sender.calls) == 1
    async with database.sessions() as session:
        delivery = await session.scalar(select(ListingDelivery))
        assert delivery.channel == "email"
        assert delivery.state == "succeeded"


async def test_manual_expired_page_does_not_process_recommended_listing(database):
    class RecommendationsOnly:
        async def scan(self, url, *, results_only=False):
            item = ParsedListing(
                url="https://qasa.com/home/different",
                external_id="different",
                rent=9000,
            )
            return BrowserScan(
                ParsedPage((item,)),
                ReadinessResult(ReadinessState.READY, "stable", ("different",)),
                url,
            )

    service = AppService(database, RecommendationsOnly(), Pipeline(database))
    with pytest.raises(IncompletePageError, match="unavailable|expired"):
        await service.process_manual("https://qasa.com/home/expired")


async def test_safe_scan_is_dry_run_and_production_scan_delivers(database):
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(
        WatcherConfig(safe_mode=True, discord={"enabled": True})
    )
    safe_result = await service.run_watcher(reason="manual-run-now")
    assert safe_result["new"] == 1
    assert safe_result["accepted"] == 1
    assert safe_result["outputs"] == "dry_run_no_outputs"
    assert output.calls == []
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(Listing.id))) == 0
        assert await session.scalar(select(func.count(ListingDelivery.id))) == 0
        assert await session.scalar(select(func.count(ManualProcessing.id))) == 0
        assert await session.scalar(select(func.count(RunListing.id))) == 0
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )
    result = await service.run_watcher(reason="manual-run-now")
    assert result["new"] == 1
    assert len(output.calls) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(Listing.id))) == 1
        delivery = await session.scalar(select(ListingDelivery))
        assert delivery.state == "succeeded"


async def test_watcher_explicitly_uses_strict_results_mode_for_any_qasa_route(database):
    browser = FakeBrowser()
    service = AppService(database, browser, Pipeline(database))
    await service.save_config(
        WatcherConfig(
            qasa_results_url="https://qasa.com/se/sv/a-future-results-route",
            safe_mode=True,
        )
    )

    await service.run_watcher(reason="manual-run-now")

    assert browser.scan_modes == [True]


async def test_safe_scan_does_not_poison_dedup_when_channel_is_enabled_later(database):
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(WatcherConfig(safe_mode=True))
    first_safe = await service.run_watcher(reason="manual-run-now")
    assert first_safe["new"] == 1
    await service.save_config(
        WatcherConfig(safe_mode=True, discord={"enabled": True})
    )
    second_safe = await service.run_watcher(reason="manual-run-now")
    assert second_safe["new"] == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(Listing.id))) == 0
        assert await session.scalar(select(func.count(ListingDelivery.id))) == 0
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )

    result = await service.run_watcher(reason="manual-run-now")

    assert result["new"] == 1
    assert len(output.calls) == 1


async def test_repeated_safe_scans_reuse_enrichment_cache_without_dedup(database):
    enricher = CountingEnricher()
    service = AppService(
        database,
        FakeBrowser(),
        Pipeline(database, enricher=enricher),
    )
    await service.save_config(WatcherConfig(safe_mode=True))

    first = await service.run_watcher(reason="manual-run-now")
    second = await service.run_watcher(reason="manual-run-now")

    assert first["new"] == second["new"] == 1
    assert enricher.calls == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(Listing.id))) == 0
        assert await session.scalar(select(func.count(ListingDelivery.id))) == 0


async def test_safe_scan_records_diagnostic_without_creating_listing_state(database):
    service = AppService(
        database,
        FakeBrowser(),
        Pipeline(database, enricher=FailingEnricher()),
    )
    await service.save_config(WatcherConfig(safe_mode=True))

    result = await service.run_watcher(reason="manual-run-now")

    assert result["failures"] == 1
    async with database.sessions() as session:
        error = await session.scalar(select(ProcessingError))
        assert error.operation == "safe_processing:manual-1"
        assert error.message == "bad maps key"
        assert await session.scalar(select(func.count(Listing.id))) == 0
        assert await session.scalar(select(func.count(RunListing.id))) == 0


async def test_safe_grouped_email_creates_no_batch_then_production_sends_once(database):
    sender = MailSender()
    grouped = EmailOutput(
        sender,
        ["reviewer@example.test"],
        mode=EmailMode.PER_SCAN,
    )
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[grouped]))
    await service.save_config(WatcherConfig(safe_mode=True, email={"enabled": True}))

    await service.run_watcher(reason="manual-run-now")

    assert sender.calls == []
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(EmailBatch.id))) == 0
        assert await session.scalar(select(func.count(Listing.id))) == 0

    await service.save_config(WatcherConfig(safe_mode=False, email={"enabled": True}))
    result = await service.run_watcher(reason="manual-run-now")

    assert result["new"] == 1
    assert len(sender.calls) == 1
    async with database.sessions() as session:
        batch = await session.scalar(select(EmailBatch))
        assert batch.state == "succeeded"


async def test_legacy_safe_tombstone_is_not_automatically_requeued(database):
    accepted = await Pipeline(database).process(
        RawListing(
            "qasa",
            "https://qasa.com/se/sv/home/manual-1",
            "manual-1",
            {"rent": 9000, "address": "Test"},
        )
    )
    async with database.sessions.begin() as session:
        session.add(
            ListingDelivery(
                listing_id=accepted.listing_id,
                channel="discord",
                state="skipped",
                last_error="suppressed by safe verification mode",
            )
        )
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )

    result = await service.run_watcher(reason="manual-run-now")

    assert result["new"] == 0
    assert output.calls == []
    async with database.sessions() as session:
        delivery = await session.scalar(select(ListingDelivery))
        assert delivery.state == "skipped"


async def test_explicit_manual_promotion_after_safe_dry_run_delivers(database):
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(
        WatcherConfig(safe_mode=True, discord={"enabled": True})
    )
    await service.run_watcher(reason="manual-run-now")
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(Listing.id))) == 0
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )
    history_id, _ = await service.process_manual("https://qasa.com/home/manual-1")

    await service.promote_manual(
        PromotionRequest(manual_id=history_id, channels=["discord"])
    )

    assert len(output.calls) == 1
    async with database.sessions() as session:
        delivery = await session.scalar(
            select(ListingDelivery).where(ListingDelivery.channel == "discord")
        )
        assert delivery.state == "succeeded"


async def test_explicit_listing_email_retry_uses_single_message_in_grouped_mode(database):
    accepted = await Pipeline(database).process(
        RawListing(
            "qasa", "https://qasa.com/home/retry-email", "retry-email", {"rent": 9000}
        )
    )
    sender = MailSender()
    grouped = EmailOutput(sender, ["reviewer@example.test"], mode=EmailMode.PER_SCAN)
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[grouped]))
    await service.save_config(WatcherConfig(safe_mode=False))

    await service.retry_listing(accepted.listing_id, ["email"])

    assert len(sender.calls) == 1


async def test_output_failure_is_visible_without_blocking_durable_listing(database):
    output = FailingOutput()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )

    result = await service.run_watcher(reason="manual-run-now")

    assert result["status"] == "completed_with_failures"
    assert result["failures"] == 1
    async with database.sessions() as session:
        run = await session.scalar(select(Run))
        listing = await session.scalar(select(Listing))
        delivery = await session.scalar(select(ListingDelivery))
        assert run.status == "failed"
        assert listing.stage == "accepted"
        assert delivery.state == "failed"


async def test_concurrent_manual_requests_return_their_own_history_ids(database):
    class DynamicBrowser:
        async def scan(self, url, *, results_only=False):
            external_id = url.rstrip("/").rsplit("/", 1)[-1]
            await asyncio.sleep(0)
            item = ParsedListing(url=url, external_id=external_id, rent=9000)
            return BrowserScan(
                ParsedPage((item,)),
                ReadinessResult(ReadinessState.READY, "ok", (external_id,)),
                url,
            )

    service = AppService(database, DynamicBrowser(), Pipeline(database))
    first, second = await asyncio.gather(
        service.process_manual("https://qasa.com/home/one"),
        service.process_manual("https://qasa.com/home/two"),
    )
    assert first[0] != second[0]
    async with database.sessions() as session:
        one = await session.get(ManualProcessing, first[0])
        two = await session.get(ManualProcessing, second[0])
        assert one.input_data["external_id"] == "one"
        assert two.input_data["external_id"] == "two"
