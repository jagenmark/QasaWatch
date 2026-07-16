"""Application service coordinating browser discovery, pipeline, and operator actions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .browser import QasaBrowser
from .config import ConfigStore
from .db import Database
from .domain import DeliveryChannel, ListingStage, RawListing
from .models import EmailBatch, Listing, ListingDelivery, ManualProcessing, ProcessingError, Run
from .pipeline import Pipeline, ProcessingOptions, ProcessingResult
from .schemas import PromotionRequest, WatcherConfig, public_config


class IncompletePageError(RuntimeError):
    """A browser page did not reach an authoritative listing/empty state."""


class AppService:
    def __init__(self, database: Database, browser: QasaBrowser, pipeline: Pipeline, *, config_store: ConfigStore | None = None, email_tester=None, pipeline_factory=None) -> None:
        self.database = database
        self.browser = browser
        self.pipeline = pipeline
        self.config_store = config_store or ConfigStore(database)
        self.email_tester = email_tester
        self.pipeline_factory = pipeline_factory
        self.scheduler = None
        self.last_browser_state: dict[str, Any] = {"status": "idle"}
        self.last_test_email: dict[str, Any] | None = None

    async def get_config(self) -> WatcherConfig:
        return WatcherConfig.model_validate(await self.config_store.get("watcher.config", {}))

    async def save_config(self, config: WatcherConfig) -> WatcherConfig:
        await self.config_store.set_value("watcher.config", config.model_dump(mode="json"))
        if self.scheduler:
            await self.scheduler.config_changed()
        return config

    async def public_config(self) -> dict[str, Any]:
        return public_config(await self.get_config())

    async def run_watcher(self, *, reason: str = "scheduled", owner: str | None = None) -> dict[str, Any]:
        config = await self.get_config()
        if not config.enabled and reason == "scheduled":
            return {"status": "disabled"}
        runtime = self.pipeline_factory(config) if self.pipeline_factory else self.pipeline
        run_id = await runtime.start_run(owner=owner)
        finish_pipeline = runtime
        processed = failures = new = accepted = rejected = 0
        output_state: Any = "dry_run_no_outputs" if config.safe_mode else "recorded_per_listing"
        found = 0
        fatal: BaseException | None = None
        try:
            scan = await self.browser.scan(config.qasa_results_url, results_only=True)
            self.last_browser_state = {
                "status": scan.readiness.state.value,
                "url": scan.final_url,
                "complete": scan.readiness.complete,
                "checked_at": datetime.now(UTC).isoformat(),
                "errors": list(scan.parsed.errors),
            }
            if not scan.readiness.complete:
                raise IncompletePageError(f"results page quarantined: {scan.readiness.state.value}")
            found = len(scan.parsed.listings)
            effective = self._pipeline_for_channels((), pipeline=runtime) if config.safe_mode else runtime
            finish_pipeline = effective
            options = ProcessingOptions(deliver=True, raise_errors=False)
            for parsed in scan.parsed.listings:
                try:
                    if config.safe_mode:
                        result = await effective.process_manual(
                            parsed.to_raw_listing(),
                            options=ProcessingOptions(
                                deliver=False,
                                record_watcher_history=False,
                                count_stats=False,
                                record_manual_history=False,
                                raise_errors=False,
                                use_enrichment_cache=True,
                            ),
                        )
                    else:
                        result = await effective.process(
                            parsed.to_raw_listing(), run_id=run_id, options=options
                        )
                    processed += 1
                    # A safe scan is a dry run: candidates remain genuinely new
                    # for the first production scan after safe mode is disabled.
                    new += int(config.safe_mode or not result.duplicate)
                    accepted += int(
                        (config.safe_mode or not result.duplicate)
                        and result.stage is ListingStage.ACCEPTED
                    )
                    rejected += int(
                        (config.safe_mode or not result.duplicate)
                        and result.stage is ListingStage.REJECTED
                    )
                    failures += len(result.delivery_failures)
                    if result.stage not in (ListingStage.ACCEPTED, ListingStage.REJECTED):
                        failures += 1
                except Exception as exc:
                    failures += 1
                    if config.safe_mode:
                        await effective.record_run_error(
                            run_id,
                            f"safe_processing:{parsed.external_id or 'unknown'}",
                            exc,
                        )
                    # Production pipeline records stage errors. A malformed
                    # individual must not abort a scan.
                    continue
        except BaseException as exc:
            fatal = exc
        async with self.database.sessions.begin() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.stats = {
                    "found": found,
                    "processed": processed,
                    "new": new,
                    "accepted": accepted,
                    "rejected": rejected,
                    "failures": failures,
                }
        run_error = fatal
        if run_error is None and failures:
            run_error = RuntimeError(
                f"scan completed with {failures} processing or delivery failure(s)"
            )
        await finish_pipeline.finish_run(run_id, error=run_error)
        if fatal:
            raise fatal
        return {"status": "completed_with_failures" if failures else "succeeded", "run_id": run_id, "found": found, "new": new, "accepted": accepted, "rejected": rejected, "failures": failures, "outputs": output_state, "safe_mode": config.safe_mode}

    async def process_manual(self, url: str, *, requested_by: str | None = None) -> tuple[int, ProcessingResult]:
        scan = await self.browser.scan(url, results_only=False)
        self.last_browser_state = {
            "status": scan.readiness.state.value,
            "url": scan.final_url,
            "complete": scan.readiness.complete,
            "checked_at": datetime.now(UTC).isoformat(),
            "errors": list(scan.parsed.errors),
        }
        if not scan.readiness.complete or not scan.parsed.listings:
            raise IncompletePageError(f"listing page incomplete: {scan.readiness.state.value}")
        expected_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        candidate = next(
            (
                item for item in scan.parsed.listings
                if item.external_id == expected_id
            ),
            None,
        )
        if candidate is None:
            raise IncompletePageError(
                "requested listing is unavailable, expired, or replaced by recommendations"
            )
        parsed_raw = candidate.to_raw_listing()
        raw = RawListing(
            parsed_raw.provider,
            parsed_raw.url,
            parsed_raw.external_id,
            {**parsed_raw.data, "detail_page_rendered": True},
        )
        runtime = self.pipeline_factory(await self.get_config()) if self.pipeline_factory else self.pipeline
        result = await runtime.process_manual(raw, requested_by=requested_by)
        assert result.manual_history_id is not None
        return result.manual_history_id, result

    async def promote_manual(self, request: PromotionRequest) -> ProcessingResult:
        async with self.database.sessions() as session:
            history = await session.get(ManualProcessing, request.manual_id)
            if history is None:
                raise LookupError(f"manual result {request.manual_id} does not exist")
            payload = dict(history.input_data)
        raw = RawListing(payload["provider"], payload["url"], payload.get("external_id"), payload.get("data", {}))
        config = await self.get_config()
        if config.safe_mode and request.channels:
            raise PermissionError("safe verification mode blocks production outputs")
        runtime = self.pipeline_factory(config) if self.pipeline_factory else self.pipeline
        # A reviewed manual promotion is a single-listing action even when the
        # watcher's normal email mode is one grouped message per scan.
        selected = self._pipeline_for_channels(
            request.channels, pipeline=runtime, per_listing_email=True
        )
        return await selected.process(
            raw,
            options=ProcessingOptions(
                deliver=bool(request.channels), allow_skipped_delivery=True
            ),
        )

    async def retry_listing(self, listing_id: int, channels: Iterable[str] = ()) -> ProcessingResult:
        config = await self.get_config()
        requested = tuple(channels)
        if config.safe_mode and requested:
            raise PermissionError("safe verification mode blocks production outputs")
        runtime = self.pipeline_factory(config) if self.pipeline_factory else self.pipeline
        return await self._pipeline_for_channels(
            requested, pipeline=runtime, per_listing_email=True
        ).resume(
            listing_id,
            options=ProcessingOptions(
                deliver=bool(requested), allow_skipped_delivery=True
            ),
        )

    async def test_email(self, recipient: str | None = None) -> dict[str, Any]:
        config = await self.get_config()
        if config.safe_mode:
            result = {"status": "blocked", "message": "safe verification mode blocks email"}
        elif not config.email.enabled:
            result = {"status": "blocked", "message": "email is disabled"}
        elif self.email_tester is None:
            result = {"status": "unavailable", "message": "email provider is not configured"}
        else:
            try:
                details = await self.email_tester(recipient or (config.email.recipients[0] if config.email.recipients else None))
                result = {"status": "succeeded", "details": details}
            except Exception as exc:
                result = {"status": "failed", "message": str(exc)[:500]}
        result["at"] = datetime.now(UTC).isoformat()
        self.last_test_email = result
        return result

    async def retry_email_batch(self, batch_id: int) -> dict[str, Any]:
        config = await self.get_config()
        if config.safe_mode:
            raise PermissionError("safe verification mode blocks production outputs")
        runtime = self.pipeline_factory(config) if self.pipeline_factory else self.pipeline
        from .emailer import DurableEmailBatcher
        provider = runtime.outputs.get(DeliveryChannel.EMAIL)
        if provider is None:
            raise RuntimeError("email provider is not configured")
        result = await DurableEmailBatcher(self.database, provider).send(batch_id)
        return {"batch_id": batch_id, "provider_message_id": result.provider_message_id, "details": dict(result.details)}

    async def resolve_email_review(self, batch_id: int, *, delivered: bool) -> None:
        config = await self.get_config()
        runtime = self.pipeline_factory(config) if self.pipeline_factory else self.pipeline
        from .emailer import DurableEmailBatcher
        provider = runtime.outputs.get(DeliveryChannel.EMAIL)
        if provider is None:
            raise RuntimeError("email provider is not configured")
        await DurableEmailBatcher(self.database, provider).resolve_manual_review(batch_id, delivered=delivered)

    async def resolve_delivery_review(
        self,
        listing_id: int,
        channel: str,
        *,
        delivered: bool,
    ) -> None:
        selected = DeliveryChannel(channel)
        if selected is DeliveryChannel.SHEETS:
            raise ValueError("Sheets deliveries are automatically idempotent and do not require manual review")
        await self.pipeline.resolve_delivery_manual_review(
            listing_id, selected, delivered=delivered
        )

    def _pipeline_for_channels(
        self,
        channels: Iterable[str],
        *,
        pipeline: Pipeline | None = None,
        per_listing_email: bool = False,
    ) -> Pipeline:
        source = pipeline or self.pipeline
        wanted = {DeliveryChannel(value) for value in channels}
        outputs = []
        for channel, provider in source.outputs.items():
            if channel not in wanted:
                continue
            if (
                per_listing_email
                and channel is DeliveryChannel.EMAIL
                and getattr(getattr(provider, "mode", None), "value", None)
                == "per_scan"
            ):
                from .emailer import EmailMode, EmailOutput

                provider = EmailOutput(
                    provider.sender,
                    provider.recipients,
                    mode=EmailMode.PER_LISTING,
                    subject_template=provider.subject_template,
                )
            outputs.append(provider)
        return Pipeline(self.database, enricher=source.enricher, filters=source.filters, outputs=outputs, enrichment_cache_ttl=source.enrichment_cache_ttl)

    async def dashboard(self) -> dict[str, Any]:
        async with self.database.sessions() as session:
            runs = list((await session.scalars(select(Run).order_by(Run.id.desc()).limit(20))).all())
            listings = list((await session.scalars(select(Listing).options(selectinload(Listing.deliveries)).order_by(Listing.id.desc()).limit(50))).all())
            errors = list((await session.scalars(select(ProcessingError).order_by(ProcessingError.id.desc()).limit(30))).all())
            manual = list((await session.scalars(select(ManualProcessing).order_by(ManualProcessing.id.desc()).limit(20))).all())
            email_batches = list((await session.scalars(select(EmailBatch).order_by(EmailBatch.id.desc()).limit(20))).all())
        email = (await self.public_config())["email"]
        pending = sum(1 for item in listings for delivery in item.deliveries if delivery.channel == "email" and delivery.state in ("pending", "failed", "manual_review")) + sum(1 for batch in email_batches if batch.state in ("pending", "retryable", "manual_review"))
        browser_state = dict(self.last_browser_state)
        health_probe = getattr(self.browser, "host_running", None)
        if health_probe is not None:
            try:
                host_running = bool(await health_probe())
            except Exception:
                host_running = False
            browser_state["host_running"] = host_running
            if not host_running:
                browser_state["last_page_status"] = browser_state.get("status")
                browser_state["status"] = "stopped"
        next_run = await self.scheduler.next_run() if self.scheduler else None
        return {
            "config": await self.public_config(),
            "watcher": {
                "running": bool(self.scheduler and self.scheduler.running),
                "lease_healthy": bool(not self.scheduler or self.scheduler.lease_healthy),
                "last_error": self.scheduler.last_error if self.scheduler else None,
                "next_run": next_run.isoformat() if next_run else None,
                "next_run_display": (
                    next_run.strftime("%A, %d %B %Y at %H:%M")
                    if next_run
                    else None
                ),
            },
            "browser": browser_state,
            "runs": runs, "listings": listings, "errors": errors, "manual": manual,
            "email": {
                **email, "pending": pending, "latest_test": self.last_test_email,
                "latest": ({"id": email_batches[0].id, "state": email_batches[0].state, "sent_at": email_batches[0].sent_at} if email_batches else None),
                "batches": [{"id": batch.id, "state": batch.state, "recipients": batch.recipients, "attempts": batch.attempts, "last_error": batch.last_error, "sent_at": batch.sent_at} for batch in email_batches],
            },
        }
