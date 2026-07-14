from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from qasawatch.domain import ListingSnapshot, ListingStage
from qasawatch.emailer import AmbiguousSMTPError, DurableEmailBatcher, EmailMode, EmailOutput, SMTPConfig, format_scan_email
from qasawatch.db import Database
from qasawatch.models import EmailBatch, Listing
from qasawatch.pipeline import Pipeline
from qasawatch.domain import RawListing
from qasawatch.enrichment import GoogleGeocoder


def listing(id=1):
    return ListingSnapshot(id, "qasa", f"https://listing/{id}", str(id), ListingStage.ACCEPTED, {"title": f"Home {id}"}, datetime.now(UTC))


class Sender:
    def __init__(self): self.calls = 0
    async def send(self, recipients, subject, body): self.calls += 1; return "message"


@pytest.mark.asyncio
async def test_success_grouped_and_test_email():
    sender = Sender(); output = EmailOutput(sender, ["a@example.com", "b@example.com"])
    result = await output.deliver_scan([listing(), listing(2)], idempotency_key="key")
    assert result.details["count"] == 2 and sender.calls == 1
    assert "Home 1" in format_scan_email([listing(), listing(2)])[1]
    await output.send_test(); assert sender.calls == 2


@pytest.mark.asyncio
async def test_failure_then_retry_semantics():
    class Flaky:
        calls = 0
        async def send(self, *args):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("fail")
            return "ok"
    sender = Flaky(); output = EmailOutput(sender, ["a@example.com"])
    with pytest.raises(RuntimeError): await output.deliver(listing(), idempotency_key="same")
    assert (await output.deliver(listing(), idempotency_key="same")).provider_message_id == "ok"


def test_invalid_config_and_redacted_credentials():
    with pytest.raises(ValueError): EmailOutput(Sender(), ["bad"])
    with pytest.raises(ValueError): SMTPConfig("smtp", 587, "bad", username="u", password="p")
    config = SMTPConfig("smtp", 587, "a@example.com", username="user", password="topsecret")
    assert "topsecret" not in repr(config) and "username='user'" not in repr(config)


@pytest.mark.asyncio
async def test_durable_batch_retry_and_ambiguous_manual_review(tmp_path):
    db = Database(tmp_path / "email.db"); await db.initialize()
    try:
        async with db.sessions.begin() as session:
            row = Listing(natural_key="k", provider="qasa", external_id="1", url="https://listing/1", stage="accepted", data={"title": "Home"}, content_hash="h")
            session.add(row); await session.flush(); listing_id = row.id
        class Ambiguous:
            async def send(self, *args): raise AmbiguousSMTPError("unknown")
        batcher = DurableEmailBatcher(db, EmailOutput(Ambiguous(), ["a@example.com"]))
        batch_id = await batcher.create([listing_id], idempotency_key="stable")
        with pytest.raises(AmbiguousSMTPError): await batcher.send(batch_id)
        async with db.sessions() as session:
            assert (await session.get(EmailBatch, batch_id)).state == "manual_review"
        await batcher.resolve_manual_review(batch_id, delivered=False)
        batcher.output.sender = Sender()
        assert (await batcher.send(batch_id)).provider_message_id == "message"
        assert (await batcher.send(batch_id)).details["duplicate"]
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_pipeline_flushes_one_durable_group_at_scan_completion(tmp_path):
    db = Database(tmp_path / "group.db"); await db.initialize()
    try:
        sender = Sender()
        pipeline = Pipeline(db, outputs=[EmailOutput(sender, ["a@example.com"], mode=EmailMode.PER_SCAN)])
        run_id = await pipeline.start_run()
        await pipeline.process(RawListing("qasa", "https://1", "1", {"title": "One"}), run_id=run_id)
        await pipeline.process(RawListing("qasa", "https://2", "2", {"title": "Two"}), run_id=run_id)
        assert sender.calls == 0
        await pipeline.finish_run(run_id)
        assert sender.calls == 1
        async with db.sessions() as session:
            batch = await session.scalar(select(EmailBatch))
            assert batch.state == "succeeded" and batch.attempts == 1
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_grouped_no_new_email_has_durable_empty_batch(tmp_path):
    db = Database(tmp_path / "empty-group.db")
    await db.initialize()
    try:
        sender = Sender()
        pipeline = Pipeline(
            db,
            outputs=[
                EmailOutput(
                    sender,
                    ["a@example.com"],
                    mode=EmailMode.PER_SCAN,
                    send_if_empty=True,
                )
            ],
        )
        run_id = await pipeline.start_run()
        await pipeline.finish_run(run_id)
        assert sender.calls == 1
        async with db.sessions() as session:
            batch = await session.scalar(select(EmailBatch))
            assert batch.state == "succeeded"
    finally:
        await db.dispose()
