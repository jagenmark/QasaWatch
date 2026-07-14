"""Generic SMTP transport and safe listing email formatting."""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from typing import Protocol, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .domain import DeliveryChannel, DeliveryResult, ListingSnapshot
from .domain import ListingStage
from .models import EmailBatch, EmailBatchListing, Listing, utcnow
from .outputs import listing_summary

_EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class EmailMode(StrEnum):
    PER_LISTING = "per_listing"
    PER_SCAN = "per_scan"


def validate_recipients(recipients: Sequence[str]) -> tuple[str, ...]:
    values = tuple(item.strip() for item in recipients)
    if not values:
        raise ValueError("at least one email recipient is required")
    invalid = [item for item in values if not _EMAIL.fullmatch(item)]
    if invalid:
        raise ValueError("invalid email recipient")
    return values


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    use_ssl: bool = False
    timeout: float = 20.0

    def __post_init__(self) -> None:
        if not self.host.strip() or not (1 <= self.port <= 65535):
            raise ValueError("valid SMTP host and port are required")
        if not _EMAIL.fullmatch(self.sender):
            raise ValueError("valid SMTP sender is required")
        if self.starttls and self.use_ssl:
            raise ValueError("SMTP STARTTLS and implicit SSL are mutually exclusive")
        if bool(self.username) != bool(self.password):
            raise ValueError("SMTP username and password must be configured together")

    def __repr__(self) -> str:
        return f"SMTPConfig(host={self.host!r}, port={self.port!r}, sender={self.sender!r}, username={'<redacted>' if self.username else None}, password={'<redacted>' if self.password else None}, starttls={self.starttls!r}, use_ssl={self.use_ssl!r}, timeout={self.timeout!r})"


class MailSender(Protocol):
    async def send(self, recipients: Sequence[str], subject: str, body: str) -> str | None: ...


class SMTPDeliveryError(RuntimeError):
    ambiguous: bool = False


class RetryableSMTPError(SMTPDeliveryError):
    """Failure happened before SMTP accepted a send operation."""


class AmbiguousSMTPError(SMTPDeliveryError):
    """SMTP send began but acknowledgement was lost; do not auto-retry."""

    ambiguous = True


class SMTPProvider:
    """SMTP adapter. Blocking stdlib I/O is moved off the event loop."""

    def __init__(self, config: SMTPConfig, *, smtp_factory=None, ssl_factory=None) -> None:
        self.config = config
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._ssl_factory = ssl_factory or smtplib.SMTP_SSL

    def __repr__(self) -> str:
        return f"{type(self).__name__}(config={self.config!r})"

    async def send(self, recipients: Sequence[str], subject: str, body: str) -> str | None:
        validated = validate_recipients(recipients)
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.config.sender, ", ".join(validated), _safe_header(subject)
        message.set_content(body)
        try:
            return await asyncio.to_thread(self._send_sync, validated, message)
        except SMTPDeliveryError:
            raise
        except Exception as exc:
            # Never include server responses or config; they can echo credentials.
            raise RetryableSMTPError(f"SMTP delivery failed before send ({type(exc).__name__})") from exc

    def _send_sync(self, recipients: tuple[str, ...], message: EmailMessage) -> str | None:
        factory = self._ssl_factory if self.config.use_ssl else self._smtp_factory
        client = factory(self.config.host, self.config.port, timeout=self.config.timeout)
        try:
            if self.config.starttls:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.config.username:
                client.login(self.config.username, self.config.password)
            try:
                refused = client.send_message(message, to_addrs=list(recipients))
            except Exception as exc:
                raise AmbiguousSMTPError(f"SMTP send outcome is ambiguous ({type(exc).__name__})") from exc
            if refused:
                # send_message may have accepted some recipients.
                raise AmbiguousSMTPError("SMTP accepted an unknown subset of recipients")
            return message.get("Message-ID")
        finally:
            try:
                client.quit()
            except Exception:
                client.close()


def _safe_header(value: str) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:200]


def format_listing_email(listing: ListingSnapshot) -> tuple[str, str]:
    data = listing.data
    summary = listing_summary(listing)
    title = summary["title"]
    subject = _safe_header(f"New Qasa listing: {title}")
    lines = [title]
    for label, key in (
        ("Address", "address"), ("Rent", "rent"), ("Rooms", "rooms"),
        ("Area", "area"), ("Coordinates", "coordinates"),
        ("Rental period", "rental_period"), ("Duration", "duration"),
        ("Availability", "availability"),
    ):
        value = summary.get(key, data.get(key))
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    lines.extend(("", listing.url))
    for label, value in (("Published", summary["published"]), ("Discovered", summary["discovered"]), ("Commute", summary["commute"]), ("Demographics", summary["demographics"]), ("Filter", summary["filter"])):
        if value:
            lines.insert(-2, f"{label}: {value}")
    return subject, "\n".join(lines)


def format_scan_email(listings: Sequence[ListingSnapshot]) -> tuple[str, str]:
    subject = f"QasaWatch: {len(listings)} new listing{'s' if len(listings) != 1 else ''}"
    if not listings:
        return "QasaWatch: no new listings", "No new listings matched this scan."
    blocks = []
    for listing in listings:
        _, body = format_listing_email(listing)
        blocks.append(body)
    return subject, "\n\n---\n\n".join(blocks)


class EmailOutput:
    channel = DeliveryChannel.EMAIL

    def __init__(self, sender: MailSender, recipients: Sequence[str], *, mode: EmailMode = EmailMode.PER_LISTING, send_if_empty: bool = False, subject_template: str | None = None) -> None:
        self.sender, self.recipients = sender, validate_recipients(recipients)
        self.mode, self.send_if_empty = EmailMode(mode), send_if_empty
        self.subject_template = subject_template.strip() if subject_template else None

    async def deliver(self, listing: ListingSnapshot, *, idempotency_key: str) -> DeliveryResult:
        subject, body = format_listing_email(listing)
        subject = self._subject(subject, count=1, listing=listing)
        message_id = await self.sender.send(self.recipients, subject, body)
        return DeliveryResult(message_id, {"idempotency_key": idempotency_key})

    async def deliver_scan(self, listings: Sequence[ListingSnapshot], *, idempotency_key: str, send_if_empty: bool = False) -> DeliveryResult:
        if not listings and not (send_if_empty or self.send_if_empty):
            return DeliveryResult(details={"skipped": True, "reason": "no_new_listings"})
        subject, body = format_scan_email(listings)
        subject = self._subject(subject, count=len(listings))
        message_id = await self.sender.send(self.recipients, subject, body)
        return DeliveryResult(message_id, {"idempotency_key": idempotency_key, "count": len(listings)})

    async def deliver_many(self, listings: Sequence[ListingSnapshot], *, idempotency_key: str) -> tuple[DeliveryResult, ...]:
        """Deliver according to the configured per-listing or grouped scan mode."""

        if self.mode is EmailMode.PER_SCAN:
            return (await self.deliver_scan(listings, idempotency_key=idempotency_key),)
        results = []
        for listing in listings:
            # The caller/durable DB should normally supply per-listing keys. This
            # suffix remains deterministic when a scan orchestrator uses this API.
            results.append(await self.deliver(listing, idempotency_key=f"{idempotency_key}:{listing.id}"))
        return tuple(results)

    async def send_test(self) -> DeliveryResult:
        message_id = await self.sender.send(self.recipients, "QasaWatch test email", "Your QasaWatch email configuration works.")
        return DeliveryResult(message_id, {"test": True})

    def _subject(
        self,
        default: str,
        *,
        count: int,
        listing: ListingSnapshot | None = None,
    ) -> str:
        if not self.subject_template:
            return default
        data = listing.data if listing else {}
        try:
            return _safe_header(
                self.subject_template.format(
                    count=count,
                    title=data.get("title") or data.get("address") or "Qasa listing",
                    address=data.get("address", ""),
                    qasa_id=listing.external_id if listing else "",
                )
            )
        except (KeyError, ValueError):
            return default

EmailDeliveryProvider = EmailOutput


class DurableEmailBatcher:
    """Persist and send grouped mail with restart-safe/manual-review semantics."""

    def __init__(self, database, output: EmailOutput) -> None:
        self.database, self.output = database, output

    async def create(self, listing_ids: Sequence[int], *, idempotency_key: str) -> int:
        ids = tuple(sorted(set(listing_ids)))
        async with self.database.sessions.begin() as session:
            inserted = await session.execute(
                sqlite_insert(EmailBatch).values(idempotency_key=idempotency_key, state="pending", recipients=list(self.output.recipients)).on_conflict_do_nothing(index_elements=[EmailBatch.idempotency_key])
            )
            batch = await session.scalar(select(EmailBatch).where(EmailBatch.idempotency_key == idempotency_key))
            assert batch is not None
            existing_ids = set(await session.scalars(select(Listing.id).where(Listing.id.in_(ids))))
            if existing_ids != set(ids):
                raise LookupError("one or more email batch listings do not exist")
            if inserted.rowcount == 1:
                for listing_id in ids:
                    await session.execute(sqlite_insert(EmailBatchListing).values(batch_id=batch.id, listing_id=listing_id).on_conflict_do_nothing(index_elements=[EmailBatchListing.batch_id, EmailBatchListing.listing_id]))
            else:
                existing = set(await session.scalars(select(EmailBatchListing.listing_id).where(EmailBatchListing.batch_id == batch.id)))
                if existing != set(ids):
                    raise ValueError("idempotency key already belongs to a different email batch")
            return batch.id

    async def send(self, batch_id: int) -> DeliveryResult:
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(EmailBatch).where(EmailBatch.id == batch_id, EmailBatch.state.in_(("pending", "retryable"))).values(state="sending", attempts=EmailBatch.attempts + 1, updated_at=utcnow())
            )
            batch = await session.get(EmailBatch, batch_id)
            if batch is None:
                raise LookupError(f"email batch {batch_id} does not exist")
            if result.rowcount != 1:
                if batch.state == "succeeded":
                    return DeliveryResult(batch.provider_message_id, {"duplicate": True, "batch_id": batch_id})
                raise RuntimeError(f"email batch {batch_id} requires manual review or is already sending")
            key, recipients = batch.idempotency_key, tuple(batch.recipients)
            ids = tuple(await session.scalars(select(EmailBatchListing.listing_id).where(EmailBatchListing.batch_id == batch_id).order_by(EmailBatchListing.listing_id)))
            rows = list(await session.scalars(select(Listing).where(Listing.id.in_(ids))))
        by_id = {row.id: row for row in rows}
        listings = [ListingSnapshot(row.id, row.provider, row.url, row.external_id, ListingStage(row.stage), dict(row.data), row.discovered_at) for row in (by_id[item] for item in ids)]
        try:
            subject, body = format_scan_email(listings)
            subject = self.output._subject(subject, count=len(listings))
            message_id = await self.output.sender.send(recipients, subject, body)
            delivered = DeliveryResult(message_id, {"idempotency_key": key, "count": len(listings), "batch_id": batch_id})
        except AmbiguousSMTPError as exc:
            await self._finish(batch_id, "manual_review", error=exc)
            raise
        except Exception as exc:
            await self._finish(batch_id, "retryable", error=exc)
            raise
        await self._finish(batch_id, "succeeded", result=delivered)
        return delivered

    async def resolve_manual_review(self, batch_id: int, *, delivered: bool) -> None:
        async with self.database.sessions.begin() as session:
            result = await session.execute(update(EmailBatch).where(EmailBatch.id == batch_id, EmailBatch.state == "manual_review").values(state="succeeded" if delivered else "retryable", updated_at=utcnow()))
            if result.rowcount != 1:
                raise RuntimeError("batch is not awaiting manual review")

    async def _finish(self, batch_id: int, state: str, *, result: DeliveryResult | None = None, error: Exception | None = None) -> None:
        async with self.database.sessions.begin() as session:
            batch = await session.get(EmailBatch, batch_id)
            assert batch is not None
            batch.state, batch.updated_at = state, utcnow()
            batch.last_error = f"{type(error).__name__}: {error}"[:1000] if error else None
            if result:
                batch.provider_message_id = result.provider_message_id
                batch.sent_at = utcnow()
