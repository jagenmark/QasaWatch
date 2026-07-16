"""Application service coordinating browser discovery, pipeline, and operator actions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from collections.abc import Mapping
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .browser import QasaBrowser
from .config import ConfigStore
from .db import Database
from .domain import DeliveryChannel, ListingStage, RawListing
from .models import (
    EmailBatch,
    Listing,
    ListingDelivery,
    ManualProcessing,
    ProcessingError,
    Run,
    utcnow,
)
from .pipeline import Pipeline, ProcessingOptions, ProcessingResult
from .redaction import redact_text
from .schemas import PromotionRequest, WatcherConfig, public_config
from .secrets import EnvironmentSecretResolver, SecretRef

STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _stockholm_time(value: datetime | str | None) -> str | None:
    """Format persisted UTC/ISO timestamps consistently for the dashboard."""

    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(STOCKHOLM).strftime("%d %B %Y, %H:%M")


def _status_label(value: str) -> str:
    return {
        "succeeded": "Completed",
        "failed": "Needs attention",
        "running": "In progress",
        "accepted": "Accepted",
        "rejected": "Filtered out",
        "discovered": "Found",
        "enriched": "Details added",
        "pending": "Waiting",
        "retryable": "Ready to retry",
        "manual_review": "Check required",
        "skipped": "Not sent",
    }.get(value, value.replace("_", " ").capitalize())


def _status_tone(value: str) -> str:
    if value in {"succeeded", "accepted"}:
        return "success"
    if value in {"failed", "rejected", "manual_review"}:
        return "danger"
    if value in {"running", "pending", "retryable", "discovered", "enriched"}:
        return "warning"
    return "neutral"


class IncompletePageError(RuntimeError):
    """A browser page did not reach an authoritative listing/empty state."""


class AppService:
    def __init__(
        self,
        database: Database,
        browser: QasaBrowser,
        pipeline: Pipeline,
        *,
        config_store: ConfigStore | None = None,
        email_tester=None,
        discord_tester=None,
        maps_tester=None,
        sheets_tester=None,
        pipeline_factory=None,
    ) -> None:
        self.database = database
        self.browser = browser
        self.pipeline = pipeline
        self.config_store = config_store or ConfigStore(database)
        self.email_tester = email_tester
        self.discord_tester = discord_tester
        self.maps_tester = maps_tester
        self.sheets_tester = sheets_tester
        self.pipeline_factory = pipeline_factory
        self.scheduler = None
        self.last_browser_state: dict[str, Any] = {"status": "idle"}
        self.last_test_email: dict[str, Any] | None = None
        self.last_test_discord: dict[str, Any] | None = None
        self.last_test_maps: dict[str, Any] | None = None
        self.last_test_sheets: dict[str, Any] | None = None

    async def get_config(self) -> WatcherConfig:
        config = WatcherConfig.model_validate(
            await self.config_store.get("watcher.config", {})
        )
        updates: dict[str, Any] = {}
        if (config.destinations or config.scb.data_path) and not config.maps_api_secret_ref:
            updates["maps_api_secret_ref"] = (
                "env:QASAWATCH_GOOGLE_MAPS_API_KEY"
            )
        if config.sheets.enabled and not config.sheets.credentials_secret_ref:
            updates["sheets"] = config.sheets.model_copy(update={
                "credentials_secret_ref": (
                    "env:QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON"
                )
            })
        if config.discord.enabled and not config.discord.webhook_secret_ref:
            updates["discord"] = config.discord.model_copy(update={
                "webhook_secret_ref": "env:QASAWATCH_DISCORD_WEBHOOK_URL"
            })
        if config.email.enabled and not config.email.smtp_secret_ref:
            updates["email"] = config.email.model_copy(update={
                "smtp_secret_ref": "env:QASAWATCH_SMTP_PASSWORD"
            })
        return config.model_copy(update=updates) if updates else config

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
        scan = None
        fatal: BaseException | None = None
        try:
            known_listing_ids: set[str] = set()
            if not config.safe_mode:
                async with self.database.sessions() as session:
                    known_listing_ids = {
                        str(value)
                        for value in (
                            await session.scalars(
                                select(Listing.external_id).where(
                                    Listing.provider == "qasa",
                                    Listing.external_id.is_not(None),
                                )
                            )
                        ).all()
                        if value
                    }
            scan = await self.browser.scan(
                config.qasa_results_url,
                results_only=True,
                known_listing_ids=known_listing_ids,
                max_pages=config.max_result_pages,
                max_listings=config.max_result_listings,
            )
            self.last_browser_state = {
                "status": scan.readiness.state.value,
                "url": scan.final_url,
                "complete": scan.readiness.complete,
                "checked_at": datetime.now(UTC).isoformat(),
                "errors": list(scan.parsed.errors),
                "pages_scanned": scan.pages_scanned,
                "total_available": scan.total_available,
                "truncated": scan.truncated,
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
                    "pages_scanned": scan.pages_scanned if scan is not None else 0,
                    "total_available": (
                        scan.total_available
                        if scan is not None and scan.total_available is not None
                        else 0
                    ),
                    "truncated": int(scan.truncated) if scan is not None else 0,
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
        assert scan is not None
        return {"status": "completed_with_failures" if failures else "succeeded", "run_id": run_id, "found": found, "pages_scanned": scan.pages_scanned, "total_available": scan.total_available, "truncated": scan.truncated, "new": new, "accepted": accepted, "rejected": rejected, "failures": failures, "outputs": output_state, "safe_mode": config.safe_mode}

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
            reviewed_result = dict(history.result or {})
            requested_by = history.requested_by
        reviewed_data = reviewed_result.get("data")
        if not isinstance(reviewed_data, dict):
            raise LookupError(
                f"manual result {request.manual_id} has no reviewed listing data"
            )
        raw = RawListing(
            payload["provider"],
            payload["url"],
            payload.get("external_id"),
            reviewed_data,
        )
        config = await self.get_config()
        if config.safe_mode and request.channels:
            raise PermissionError("safe verification mode blocks production outputs")
        runtime = self.pipeline_factory(config) if self.pipeline_factory else self.pipeline
        async with self.database.sessions.begin() as session:
            promotion = ManualProcessing(
                action="promote",
                status="running",
                requested_by=requested_by,
                input_data={
                    "source_manual_id": request.manual_id,
                    "provider": raw.provider,
                    "url": raw.url,
                    "external_id": raw.external_id,
                    "channels": list(request.channels),
                },
            )
            session.add(promotion)
            await session.flush()
            promotion_id = promotion.id
        # A reviewed manual promotion is a single-listing action even when the
        # watcher's normal email mode is one grouped message per scan.
        selected = self._pipeline_for_channels(
            request.channels, pipeline=runtime, per_listing_email=True
        )
        try:
            result = await selected.promote_enriched(
                raw,
                options=ProcessingOptions(
                    deliver=bool(request.channels),
                    allow_skipped_delivery=True,
                    force_delivery=True,
                ),
            )
            statuses = await self._promotion_delivery_statuses(
                result.listing_id, request.channels
            )
            result = replace(result, delivery_statuses=statuses)
            async with self.database.sessions.begin() as session:
                promotion = await session.get(ManualProcessing, promotion_id)
                if promotion is not None:
                    promotion.listing_id = result.listing_id
                    promotion.status = (
                        "failed"
                        if any(
                            item.get("outcome") not in {"sent", "already_sent"}
                            for item in statuses.values()
                        )
                        else "succeeded"
                    )
                    promotion.result = {
                        "listing_id": result.listing_id,
                        "stage": result.stage.value,
                        "duplicate": result.duplicate,
                        "delivery_failures": list(result.delivery_failures),
                        "delivery_statuses": statuses,
                    }
                    promotion.finished_at = utcnow()
            return result
        except BaseException as exc:
            async with self.database.sessions.begin() as session:
                promotion = await session.get(ManualProcessing, promotion_id)
                if promotion is not None:
                    promotion.status = "failed"
                    promotion.error = redact_text(exc)
                    promotion.finished_at = utcnow()
            raise

    async def _promotion_delivery_statuses(
        self,
        listing_id: int | None,
        channels: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        requested = tuple(channels)
        if listing_id is None or not requested:
            return {}
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ListingDelivery).where(
                        ListingDelivery.listing_id == listing_id,
                        ListingDelivery.channel.in_(requested),
                    )
                )
            ).all()
        by_channel = {row.channel: row for row in rows}
        statuses: dict[str, dict[str, Any]] = {}
        for channel in requested:
            row = by_channel.get(channel)
            state = row.state if row is not None else "not_configured"
            outcome = (
                "sent"
                if state == "succeeded"
                else state
            )
            statuses[channel] = {
                "state": state,
                "outcome": outcome,
                "message": (
                    redact_text(row.last_error)
                    if row is not None and row.last_error
                    else None
                ),
                "delivered_at": (
                    row.delivered_at.isoformat()
                    if row is not None and row.delivered_at
                    else None
                ),
            }
        return statuses

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
        secret = self._resolved_secret(config.email.smtp_secret_ref)
        if config.safe_mode:
            result = {"status": "blocked", "message": "safe verification mode blocks email"}
        elif not config.email.enabled:
            result = {"status": "blocked", "message": "email is disabled"}
        elif not secret and self.email_tester is None:
            result = {"status": "unavailable", "message": "Email credentials are not available"}
        elif self.email_tester is None:
            result = {"status": "unavailable", "message": "email provider is not configured"}
        else:
            try:
                details = await self.email_tester(recipient or (config.email.recipients[0] if config.email.recipients else None))
                result = {
                    "status": "succeeded",
                    "details": self._redacted_value(
                        details, self._secret_markers(secret)
                    ),
                }
            except Exception as exc:
                result = {
                    "status": "failed",
                    "message": redact_text(
                        exc, self._secret_markers(secret)
                    )[:500],
                }
        result["at"] = datetime.now(UTC).isoformat()
        self.last_test_email = result
        return result

    async def test_discord(self) -> dict[str, Any]:
        config = await self.get_config()
        secret = self._resolved_secret(config.discord.webhook_secret_ref)
        if (
            (not secret or not secret.startswith("https://"))
            and self.discord_tester is None
        ):
            result = {"status": "unavailable", "message": "Discord is not connected"}
        elif self.discord_tester is None:
            result = {"status": "unavailable", "message": "Discord test is unavailable"}
        else:
            try:
                details = await self.discord_tester()
                result = {
                    "status": "succeeded",
                    "details": self._redacted_value(
                        details, self._secret_markers(secret)
                    ),
                }
            except Exception as exc:
                result = {
                    "status": "failed",
                    "message": redact_text(
                        exc, self._secret_markers(secret)
                    )[:500],
                }
        result["at"] = datetime.now(UTC).isoformat()
        self.last_test_discord = result
        return result

    async def test_maps(self) -> dict[str, Any]:
        config = await self.get_config()
        secret = self._resolved_secret(config.maps_api_secret_ref)
        if not secret and self.maps_tester is None:
            result = {"status": "unavailable", "message": "Google Maps is not connected"}
        elif self.maps_tester is None:
            result = {"status": "unavailable", "message": "Google Maps test is unavailable"}
        else:
            try:
                details = await self.maps_tester()
                result = {
                    "status": "succeeded",
                    "details": self._redacted_value(
                        details, self._secret_markers(secret)
                    ),
                }
            except Exception as exc:
                result = {
                    "status": "failed",
                    "message": redact_text(
                        exc, self._secret_markers(secret)
                    )[:500],
                }
        result["at"] = datetime.now(UTC).isoformat()
        self.last_test_maps = result
        return result

    async def test_sheets(self) -> dict[str, Any]:
        config = await self.get_config()
        secret = self._resolved_secret(config.sheets.credentials_secret_ref)
        if not config.sheets.enabled:
            result = {"status": "blocked", "message": "Google Sheets is disabled"}
        elif not secret and self.sheets_tester is None:
            result = {
                "status": "unavailable",
                "message": "Google Sheets credentials are not available",
            }
        elif not config.sheets.spreadsheet_id.strip():
            result = {
                "status": "unavailable",
                "message": "Google Sheets spreadsheet is not configured",
            }
        elif self.sheets_tester is None:
            result = {"status": "unavailable", "message": "Google Sheets test is unavailable"}
        else:
            try:
                details = await self.sheets_tester()
                result = {
                    "status": "succeeded",
                    "details": self._redacted_value(
                        details, self._secret_markers(secret)
                    ),
                }
            except Exception as exc:
                result = {
                    "status": "failed",
                    "message": redact_text(
                        exc, self._secret_markers(secret)
                    )[:500],
                }
        result["at"] = datetime.now(UTC).isoformat()
        self.last_test_sheets = result
        return result

    @staticmethod
    def _resolved_secret(reference: str | None) -> str | None:
        if not reference:
            return None
        try:
            value = EnvironmentSecretResolver().resolve(SecretRef.parse(reference))
        except Exception:
            return None
        return value if value and value.strip() else None

    @staticmethod
    def _secret_markers(secret: str | None) -> tuple[str, ...]:
        if not secret:
            return ()
        markers = {secret}
        if secret.startswith(("http://", "https://")):
            markers.update(
                part for part in urlparse(secret).path.split("/") if len(part) >= 8
            )
        return tuple(markers)

    @classmethod
    def _redacted_value(cls, value: Any, secrets: tuple[str, ...]) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): (
                    "<redacted>"
                    if any(
                        marker in str(key).lower()
                        for marker in ("password", "secret", "token", "api_key", "credential")
                    )
                    else cls._redacted_value(item, secrets)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._redacted_value(item, secrets) for item in value]
        if isinstance(value, str):
            return redact_text(value, secrets)
        return value

    @staticmethod
    def _test_view(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {
            **value,
            "status_label": _status_label(str(value.get("status", ""))),
            "status_tone": _status_tone(str(value.get("status", ""))),
            "at_display": _stockholm_time(value.get("at")),
            "message": redact_text(value.get("message")) if value.get("message") else None,
        }

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
        public = await self.public_config()
        email = public["email"]
        discord = public["discord"]
        config = await self.get_config()
        maps_needed = bool(config.destinations or config.scb.data_path)
        maps_secret = self._resolved_secret(config.maps_api_secret_ref)
        sheets_secret = self._resolved_secret(config.sheets.credentials_secret_ref)
        discord_secret = self._resolved_secret(config.discord.webhook_secret_ref)
        email_secret = self._resolved_secret(config.email.smtp_secret_ref)

        maps_connected = bool(maps_secret)
        sheets_connected = bool(
            sheets_secret
            and config.sheets.spreadsheet_id.strip()
            and config.sheets.worksheet.strip()
        )
        discord_connected = bool(
            discord_secret and discord_secret.startswith("https://")
        )
        email_connected = bool(
            email_secret and config.email.smtp_host.strip() and config.email.sender.strip()
        )

        def connection_state(
            *,
            name: str,
            connected: bool,
            can_test: bool,
            enabled: bool | None = None,
            needed: bool | None = None,
            connected_message: str,
            missing_message: str,
            latest_test: dict[str, Any] | None,
        ) -> dict[str, Any]:
            inactive = enabled is False or needed is False
            test_view = self._test_view(latest_test)
            test_status = (
                str(test_view.get("status", "")) if test_view else None
            )
            return {
                "name": name,
                "connected": connected,
                "can_test": can_test,
                **({"enabled": enabled} if enabled is not None else {}),
                **({"needed": needed} if needed is not None else {}),
                "status_label": (
                    "Off"
                    if enabled is False
                    else "Not needed"
                    if needed is False
                    else "Needs attention"
                    if test_status == "failed"
                    else "Connected"
                    if test_status == "succeeded"
                    else "Ready to test"
                    if connected
                    else "Needs setup"
                ),
                "status_tone": (
                    "neutral"
                    if inactive
                    else "danger"
                    if test_status == "failed"
                    else "success"
                    if test_status == "succeeded"
                    else "warning"
                    if connected
                    else "danger"
                ),
                "message": connected_message if connected else missing_message,
                "latest_test": test_view,
            }

        connections = {
            "maps": connection_state(
                name="Google Maps",
                connected=maps_connected,
                can_test=bool(self.maps_tester and maps_connected and maps_needed),
                needed=maps_needed,
                connected_message="Google Maps credentials are available in the running environment",
                missing_message=(
                    "Google Maps is not currently needed"
                    if not maps_needed
                    else "The Google Maps credential was not found in the running app"
                ),
                latest_test=self.last_test_maps,
            ),
            "sheets": connection_state(
                name="Google Sheets",
                connected=sheets_connected,
                can_test=bool(
                    self.sheets_tester
                    and config.sheets.enabled
                    and sheets_connected
                ),
                enabled=config.sheets.enabled,
                connected_message="Google Sheets credentials and destination are available",
                missing_message=(
                    "Google Sheets is disabled"
                    if not config.sheets.enabled
                    else "Google Sheets credentials or spreadsheet settings are unavailable"
                ),
                latest_test=self.last_test_sheets,
            ),
            "discord": connection_state(
                name="Discord",
                connected=discord_connected,
                can_test=bool(self.discord_tester and discord_connected),
                enabled=config.discord.enabled,
                connected_message="Webhook available in the running environment",
                missing_message=(
                    "Discord is disabled"
                    if not config.discord.enabled
                    else "The Discord webhook was not found or is not a valid HTTPS URL"
                ),
                latest_test=self.last_test_discord,
            ),
            "email": connection_state(
                name="Email",
                connected=email_connected,
                can_test=bool(
                    self.email_tester
                    and config.email.enabled
                    and email_connected
                    and config.email.recipients
                ),
                enabled=config.email.enabled,
                connected_message="Email credentials and server settings are available",
                missing_message=(
                    "Email is disabled"
                    if not config.email.enabled
                    else "Email credentials or server settings are unavailable"
                ),
                latest_test=self.last_test_email,
            ),
        }
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
        browser_state["checked_display"] = _stockholm_time(
            browser_state.get("checked_at")
        )
        next_run = await self.scheduler.next_run() if self.scheduler else None
        run_views = [
            {
                "id": run.id,
                "status": run.status,
                "status_label": _status_label(run.status),
                "status_tone": _status_tone(run.status),
                "started_display": _stockholm_time(run.started_at),
                "finished_display": _stockholm_time(run.finished_at),
                "stats": dict(run.stats or {}),
                "error": redact_text(run.error) if run.error else None,
            }
            for run in runs
        ]
        error_views = [
            {
                "id": error.id,
                "run_id": error.run_id,
                "listing_id": error.listing_id,
                "occurred_display": _stockholm_time(error.created_at),
                "operation": error.operation,
                "error_type": error.error_type,
                "message": redact_text(error.message),
            }
            for error in errors
        ]
        listing_views = [
            {
                "id": item.id,
                "url": item.url,
                "external_id": item.external_id,
                "address": item.data.get("address") or item.external_id or item.id,
                "stage": item.stage,
                "stage_label": _status_label(item.stage),
                "stage_tone": _status_tone(item.stage),
                "discovered_display": _stockholm_time(item.discovered_at),
                "rejection_reasons": item.rejection_reasons,
                "deliveries": [
                    {
                        "channel": delivery.channel,
                        "channel_label": {
                            "discord": "Discord",
                            "email": "Email",
                            "sheets": "Google Sheets",
                        }.get(delivery.channel, delivery.channel.capitalize()),
                        "state": delivery.state,
                        "state_label": _status_label(delivery.state),
                        "state_tone": _status_tone(delivery.state),
                    }
                    for delivery in item.deliveries
                ],
            }
            for item in listings
        ]
        manual_views = [
            {
                "id": item.id,
                "action": item.action,
                "action_label": (
                    "Send reviewed listing"
                    if item.action == "promote"
                    else "Check listing"
                ),
                "status": item.status,
                "status_label": _status_label(item.status),
                "status_tone": _status_tone(item.status),
                "requested_display": _stockholm_time(item.started_at),
                "url": item.input_data.get("url", ""),
                "channels": list(item.input_data.get("channels") or []),
                "result": item.result,
                "delivery_statuses": dict(
                    (item.result or {}).get("delivery_statuses") or {}
                ),
                "error": redact_text(item.error) if item.error else None,
            }
            for item in manual
        ]
        latest_test_display = connections["email"]["latest_test"]
        return {
            "config": await self.public_config(),
            "connections": connections,
            "watcher": {
                "running": bool(self.scheduler and self.scheduler.running),
                "lease_healthy": bool(not self.scheduler or self.scheduler.lease_healthy),
                "last_error": self.scheduler.last_error if self.scheduler else None,
                "next_run": next_run.isoformat() if next_run else None,
                "next_run_display": (
                    _stockholm_time(next_run) if next_run else None
                ),
            },
            "browser": browser_state,
            "runs": runs, "listings": listings, "errors": errors, "manual": manual,
            "run_views": run_views,
            "listing_views": listing_views,
            "error_views": error_views,
            "manual_views": manual_views,
            "discord": {
                **discord,
                "connected": discord_connected,
                "connection_message": connections["discord"]["message"],
                "latest_test": connections["discord"]["latest_test"],
            },
            "email": {
                **email,
                "connected": email_connected,
                "connection_message": connections["email"]["message"],
                "pending": pending,
                "latest_test": latest_test_display,
                "latest_test_display": (
                    f"{latest_test_display['status_label']} · "
                    f"{latest_test_display['at_display']}"
                    if latest_test_display
                    else None
                ),
                "latest": (
                    {
                        "id": email_batches[0].id,
                        "state": email_batches[0].state,
                        "state_label": _status_label(email_batches[0].state),
                        "sent_display": _stockholm_time(email_batches[0].sent_at),
                    }
                    if email_batches
                    else None
                ),
                "latest_display": (
                    _stockholm_time(email_batches[0].sent_at)
                    if email_batches and email_batches[0].sent_at
                    else None
                ),
                "batches": [
                    {
                        "id": batch.id,
                        "state": batch.state,
                        "state_label": _status_label(batch.state),
                        "state_tone": _status_tone(batch.state),
                        "recipients": batch.recipients,
                        "attempts": batch.attempts,
                        "last_error": batch.last_error,
                        "sent_display": _stockholm_time(batch.sent_at),
                    }
                    for batch in email_batches
                ],
            },
        }
