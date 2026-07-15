import asyncio

import pytest
from sqlalchemy import func, select

from qasawatch.browser import BrowserScan
from qasawatch.db import Database
from qasawatch.domain import DeliveryChannel, DeliveryResult, RawListing
from qasawatch.emailer import EmailMode, EmailOutput
from qasawatch.filters import FilterChain, NumericRangeFilter
from qasawatch.models import Listing, ListingDelivery, ManualProcessing, Run
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


class MailSender:
    def __init__(self): self.calls = []
    async def send(self, recipients, subject, body):
        self.calls.append((tuple(recipients), subject, body))
        return "mail-1"


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


async def test_safe_scan_does_not_backfill_outputs_when_production_is_enabled(database):
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(
        WatcherConfig(safe_mode=True, discord={"enabled": True})
    )
    await service.run_watcher(reason="manual-run-now")
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )
    result = await service.run_watcher(reason="manual-run-now")
    assert result["new"] == 0
    assert output.calls == []
    async with database.sessions() as session:
        delivery = await session.scalar(select(ListingDelivery))
        assert delivery.state == "skipped"


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


async def test_safe_scan_tombstones_channels_enabled_only_later(database):
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(WatcherConfig(safe_mode=True))
    await service.run_watcher(reason="manual-run-now")
    await service.save_config(
        WatcherConfig(safe_mode=True, discord={"enabled": True})
    )
    await service.run_watcher(reason="manual-run-now")
    await service.save_config(
        WatcherConfig(safe_mode=False, discord={"enabled": True})
    )

    result = await service.run_watcher(reason="manual-run-now")

    assert result["new"] == 0
    assert output.calls == []


async def test_explicit_manual_promotion_overrides_safe_tombstone(database):
    output = Output()
    service = AppService(database, FakeBrowser(), Pipeline(database, outputs=[output]))
    await service.save_config(
        WatcherConfig(safe_mode=True, discord={"enabled": True})
    )
    await service.run_watcher(reason="manual-run-now")
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
