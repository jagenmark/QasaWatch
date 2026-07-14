import pytest
from sqlalchemy import func, select

from qasawatch.db import Database
from qasawatch.domain import (
    DeliveryChannel,
    DeliveryResult,
    DeliveryState,
    EnrichedListing,
    ListingStage,
    RawListing,
)
from qasawatch.filters import FilterChain, NumericRangeFilter
from qasawatch.models import (
    DeliveryAttempt,
    Listing,
    ListingDelivery,
    ManualProcessing,
    ProcessingError,
    ProcessingEvent,
    Run,
)
from qasawatch.pipeline import Pipeline, ProcessingOptions
from qasawatch.outputs import AmbiguousOutputError


@pytest.fixture
async def database(tmp_path):
    db = Database(tmp_path / "state.db")
    await db.initialize()
    yield db
    await db.dispose()


class FailingEnricher:
    name = "details"

    async def enrich(self, listing):
        raise RuntimeError("temporary browser failure")


class WorkingEnricher:
    name = "details"

    def __init__(self):
        self.calls = 0

    async def enrich(self, listing):
        self.calls += 1
        return EnrichedListing(
            listing.provider,
            listing.url,
            listing.external_id,
            {**listing.data, "rent": 9_000, "rooms": 2},
        )


class NamespacedEnricher(WorkingEnricher):
    def __init__(self, namespace, marker):
        super().__init__()
        self.cache_namespace = namespace
        self.marker = marker

    async def enrich(self, listing):
        value = await super().enrich(listing)
        return EnrichedListing(
            value.provider,
            value.url,
            value.external_id,
            {**value.data, "destination_marker": self.marker},
        )


class RecordingOutput:
    channel = DeliveryChannel.DISCORD

    def __init__(self):
        self.keys = []

    async def deliver(self, listing, *, idempotency_key):
        self.keys.append(idempotency_key)
        return DeliveryResult(provider_message_id="message-1")


class FailsOnceOutput(RecordingOutput):
    channel = DeliveryChannel.SHEETS

    async def deliver(self, listing, *, idempotency_key):
        self.keys.append(idempotency_key)
        if len(self.keys) == 1:
            raise RuntimeError("response lost")
        return DeliveryResult(provider_message_id="sheet-row-1")


async def test_duplicate_detection_is_by_provider_and_external_id(database):
    pipeline = Pipeline(database)
    raw = RawListing("qasa", "https://example.test/first", "listing-42", {"rent": 9000})

    first = await pipeline.process(raw)
    duplicate = await pipeline.process(
        RawListing("qasa", "https://example.test/url-changed", "listing-42", {"rent": 9999})
    )

    assert not first.duplicate
    assert duplicate.duplicate
    assert duplicate.listing_id == first.listing_id


async def test_enrichment_cache_is_scoped_to_listing_identity(database):
    enricher = WorkingEnricher()
    pipeline = Pipeline(database, enricher=enricher)
    first = await pipeline.process(
        RawListing("qasa", "https://qasa.com/home/1", "1", {})
    )
    second = await pipeline.process(
        RawListing("qasa", "https://qasa.com/home/2", "2", {})
    )
    assert enricher.calls == 2
    assert (await pipeline.snapshot(first.listing_id)).external_id == "1"
    assert (await pipeline.snapshot(second.listing_id)).external_id == "2"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(Listing.id))) == 2


async def test_enrichment_cache_is_scoped_to_configuration_namespace(database):
    raw = RawListing("qasa", "https://qasa.com/home/1", "1", {"rent": 9000})
    source_hash = Pipeline.content_hash(raw.data)
    old = NamespacedEnricher("destinations:old", "old")
    new = NamespacedEnricher("destinations:new", "new")

    first = await Pipeline(database, enricher=old)._enrich_raw(raw, source_hash)
    second = await Pipeline(database, enricher=new)._enrich_raw(raw, source_hash)

    assert first.data["destination_marker"] == "old"
    assert second.data["destination_marker"] == "new"
    assert old.calls == 1 and new.calls == 1


async def test_enrichment_crash_leaves_discovered_and_restart_resumes(database):
    raw = RawListing("qasa", "https://example.test/42", "42", {"summary": "home"})
    with pytest.raises(RuntimeError, match="temporary browser"):
        await Pipeline(database, enricher=FailingEnricher()).process(raw)

    async with database.sessions() as session:
        listing = await session.scalar(select(Listing))
        assert listing.stage == ListingStage.DISCOVERED.value
        assert await session.scalar(select(func.count(ProcessingError.id))) == 1

    enricher = WorkingEnricher()
    restarted = Pipeline(database, enricher=enricher)
    result = await restarted.resume(listing.id)
    assert result.stage is ListingStage.ACCEPTED
    assert enricher.calls == 1


async def test_rejection_persists_structured_reason(database):
    pipeline = Pipeline(
        database,
        filters=FilterChain([NumericRangeFilter("rent", maximum=10_300, name="budget")]),
    )
    result = await pipeline.process(
        RawListing("qasa", "https://example.test/expensive", "expensive", {"rent": 12_000})
    )

    assert result.stage is ListingStage.REJECTED
    async with database.sessions() as session:
        listing = await session.get(Listing, result.listing_id)
        assert listing.rejection_reasons[0]["code"] == "rent.above_maximum"
        assert listing.rejection_reasons[0]["source"] == "machine"


async def test_recovery_requires_review_then_reuses_delivery_idempotency_key(database):
    output = RecordingOutput()
    pipeline = Pipeline(database, outputs=[output])
    # Create watcher state while suppressing output to emulate a pre-send crash.
    result = await pipeline.process(
        RawListing("qasa", "https://example.test/output", "output", {}),
        options=ProcessingOptions(deliver=False),
    )
    assert result.listing_id is not None
    async with database.sessions.begin() as session:
        delivery = ListingDelivery(
            listing_id=result.listing_id,
            channel=DeliveryChannel.DISCORD.value,
            state=DeliveryState.IN_PROGRESS.value,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            DeliveryAttempt(
                delivery_id=delivery.id,
                sequence=1,
                idempotency_key=f"qasawatch:{result.listing_id}:discord",
                state=DeliveryState.IN_PROGRESS.value,
            )
        )

    await database.recover_interrupted_work()
    await pipeline.resume(result.listing_id)

    assert output.keys == []
    async with database.sessions() as session:
        delivery = await session.scalar(select(ListingDelivery))
        attempt = await session.scalar(select(DeliveryAttempt))
        assert delivery.state == DeliveryState.MANUAL_REVIEW.value
        assert attempt.state == DeliveryState.MANUAL_REVIEW.value

    await pipeline.resolve_delivery_manual_review(
        result.listing_id, DeliveryChannel.DISCORD, delivered=False
    )
    await pipeline.resume(result.listing_id)

    assert output.keys == [f"qasawatch:{result.listing_id}:discord"]
    async with database.sessions() as session:
        delivery = await session.scalar(select(ListingDelivery))
        attempt = await session.scalar(
            select(DeliveryAttempt).order_by(DeliveryAttempt.sequence.desc())
        )
        assert delivery.state == DeliveryState.SUCCEEDED.value
        assert attempt.state == DeliveryState.SUCCEEDED.value
        assert attempt.sequence == 2
        assert attempt.idempotency_key == f"qasawatch:{result.listing_id}:discord"


async def test_ambiguous_webhook_failure_waits_for_manual_review(database):
    class AmbiguousOutput:
        channel = DeliveryChannel.DISCORD

        async def deliver(self, listing, *, idempotency_key):
            raise AmbiguousOutputError("response was lost")

    result = await Pipeline(database, outputs=[AmbiguousOutput()]).process(
        RawListing("qasa", "https://example.test/ambiguous", "ambiguous", {}),
        options=ProcessingOptions(raise_errors=False),
    )
    async with database.sessions() as session:
        delivery = await session.scalar(
            select(ListingDelivery).where(ListingDelivery.listing_id == result.listing_id)
        )
        assert delivery.state == DeliveryState.MANUAL_REVIEW.value


async def test_manual_defaults_have_separate_history_but_no_watcher_history_or_output(database):
    output = RecordingOutput()
    result = await Pipeline(database, outputs=[output]).process_manual(
        RawListing("qasa", "https://example.test/manual", "manual", {})
    )

    assert result.stage is ListingStage.ACCEPTED
    assert output.keys == []
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(ManualProcessing.id))) == 1
        assert await session.scalar(select(func.count(ProcessingEvent.id))) == 0
        assert await session.scalar(select(func.count(Run.id))) == 0
        assert await session.scalar(select(func.count(ListingDelivery.id))) == 0
        assert await session.scalar(select(func.count(Listing.id))) == 0


async def test_output_failure_does_not_block_other_channel_and_retry_key_is_stable(database):
    failing = FailsOnceOutput()
    successful = RecordingOutput()
    raw = RawListing("qasa", "https://example.test/independent", "independent", {})
    pipeline = Pipeline(database, outputs=[failing, successful])

    with pytest.raises(ExceptionGroup):
        await pipeline.process(raw)
    assert len(successful.keys) == 1

    retried = await pipeline.process(raw)
    assert retried.duplicate
    assert failing.keys == [
        f"qasawatch:{retried.listing_id}:sheets",
        f"qasawatch:{retried.listing_id}:sheets",
    ]
    # The already-succeeded Discord operation is not sent again.
    assert len(successful.keys) == 1
    async with database.sessions() as session:
        attempts = (
            await session.scalars(
                select(DeliveryAttempt).order_by(DeliveryAttempt.sequence)
            )
        ).all()
        sheets_attempts = [attempt for attempt in attempts if "sheets" in attempt.idempotency_key]
        assert [attempt.sequence for attempt in sheets_attempts] == [1, 2]
        assert len({attempt.idempotency_key for attempt in sheets_attempts}) == 1
