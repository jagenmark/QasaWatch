"""Durable stage machine shared by watcher and manual processing paths."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import Database
from .domain import (
    DeliveryChannel,
    DeliveryProvider,
    DeliveryState,
    EnrichedListing,
    EnrichmentProvider,
    FilterDecision,
    ListingSnapshot,
    ListingStage,
    RawListing,
    RunKind,
    RunStatus,
)
from .filters import FilterChain
from .models import (
    DeliveryAttempt,
    EnrichmentCache,
    Listing,
    ListingDelivery,
    ManualProcessing,
    ProcessingError,
    ProcessingEvent,
    Run,
    RunListing,
    utcnow,
)


@dataclass(frozen=True, slots=True)
class ProcessingOptions:
    deliver: bool = True
    record_watcher_history: bool = True
    count_stats: bool = True
    record_manual_history: bool = False
    raise_errors: bool = True
    allow_skipped_delivery: bool = False

    @classmethod
    def manual(cls) -> "ProcessingOptions":
        return cls(
            deliver=False,
            record_watcher_history=False,
            count_stats=False,
            record_manual_history=True,
        )


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    listing_id: int | None
    stage: ListingStage
    duplicate: bool
    decision: FilterDecision | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    manual_history_id: int | None = None
    delivery_failures: tuple[str, ...] = ()


class Pipeline:
    def __init__(
        self,
        database: Database,
        *,
        enricher: EnrichmentProvider | None = None,
        filters: FilterChain | None = None,
        outputs: Iterable[DeliveryProvider] = (),
        enrichment_cache_ttl: timedelta = timedelta(hours=24),
    ) -> None:
        self.database = database
        self.enricher = enricher
        self.filters = filters or FilterChain()
        providers = tuple(outputs)
        channels = [provider.channel for provider in providers]
        if len(set(channels)) != len(channels):
            raise ValueError("only one output provider may own each delivery channel")
        self.outputs = {provider.channel: provider for provider in providers}
        self.enrichment_cache_ttl = enrichment_cache_ttl

    async def start_run(self, *, owner: str | None = None) -> int:
        async with self.database.sessions.begin() as session:
            run = Run(kind=RunKind.WATCHER.value, status=RunStatus.RUNNING.value, owner=owner)
            session.add(run)
            await session.flush()
            return run.id

    async def finish_run(self, run_id: int, *, error: BaseException | None = None) -> None:
        final_error = error
        try:
            # Grouped email is independent of per-listing output failures.
            await self._flush_grouped_email(run_id)
        except BaseException as exc:
            if final_error is None:
                final_error = exc
        async with self.database.sessions.begin() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            run.status = (RunStatus.FAILED if final_error else RunStatus.SUCCEEDED).value
            run.finished_at = utcnow()
            run.error = self._error_message(final_error) if final_error else None
        if final_error is not None and error is None:
            raise final_error

    def _is_grouped_email(self, channel: DeliveryChannel, provider: DeliveryProvider) -> bool:
        mode = getattr(provider, "mode", None)
        return channel is DeliveryChannel.EMAIL and getattr(mode, "value", mode) == "per_scan"

    async def _flush_grouped_email(self, run_id: int) -> None:
        provider = self.outputs.get(DeliveryChannel.EMAIL)
        if provider is None or not self._is_grouped_email(DeliveryChannel.EMAIL, provider):
            return
        from .emailer import DurableEmailBatcher
        from .outputs import grouped_idempotency_key

        async with self.database.sessions() as session:
            listing_ids = tuple(await session.scalars(
                select(RunListing.listing_id)
                .join(Listing, Listing.id == RunListing.listing_id)
                .where(RunListing.run_id == run_id, RunListing.duplicate.is_(False), Listing.stage == ListingStage.ACCEPTED.value)
                .order_by(RunListing.listing_id)
            ))
        if not listing_ids and not getattr(provider, "send_if_empty", False):
            return
        key = grouped_idempotency_key(listing_ids, DeliveryChannel.EMAIL, scan_id=run_id)
        batcher = DurableEmailBatcher(self.database, provider)  # type: ignore[arg-type]
        batch_id = await batcher.create(listing_ids, idempotency_key=key)
        await batcher.send(batch_id)

    async def process(
        self,
        raw: RawListing,
        *,
        run_id: int | None = None,
        options: ProcessingOptions | None = None,
    ) -> ProcessingResult:
        opts = options or ProcessingOptions()
        listing_id, duplicate = await self._discover(raw, run_id=run_id, options=opts)
        try:
            result = await self.resume(listing_id, run_id=run_id, options=opts)
        except BaseException as exc:
            if opts.raise_errors:
                raise
            async with self.database.sessions() as session:
                listing = await session.get(Listing, listing_id)
                assert listing is not None
                return ProcessingResult(listing_id, ListingStage(listing.stage), duplicate)
        await self._update_run_listing(run_id, listing_id, result.stage, options=opts)
        if opts.count_stats and run_id is not None:
            await self._increment_stat(run_id, "duplicates" if duplicate else "discovered")
            await self._increment_stat(run_id, result.stage.value)
        return ProcessingResult(
            listing_id=result.listing_id,
            stage=result.stage,
            duplicate=duplicate,
            decision=result.decision,
            data=result.data,
            delivery_failures=result.delivery_failures,
        )

    async def process_manual(
        self,
        raw: RawListing,
        *,
        requested_by: str | None = None,
        options: ProcessingOptions | None = None,
        promote: bool = False,
    ) -> ProcessingResult:
        """Process transiently, unless explicitly promoted into watcher state.

        Manual inspection must not poison watcher deduplication.  Its enriched
        payload and decision are retained only in ``manual_processing``.
        """

        opts = options or ProcessingOptions.manual()
        if promote:
            return await self.process(raw, options=opts)
        history_id: int | None = None
        if opts.record_manual_history:
            async with self.database.sessions.begin() as session:
                history = ManualProcessing(
                    status=RunStatus.RUNNING.value,
                    requested_by=requested_by,
                    input_data=self._raw_dict(raw),
                )
                session.add(history)
                await session.flush()
                history_id = history.id
        try:
            enriched = await self._enrich_raw(
                raw, self.content_hash(raw.data), use_cache=False
            )
            decision = await self.filters.evaluate(enriched)
            stage = ListingStage.ACCEPTED if decision.accepted else ListingStage.REJECTED
            result = ProcessingResult(
                None,
                stage,
                duplicate=False,
                decision=decision,
                data=dict(enriched.data),
            )
        except BaseException as exc:
            if history_id is not None:
                await self._finish_manual(history_id, error=exc)
            raise
        if history_id is not None:
            await self._finish_manual(history_id, result=result)
            result = replace(result, manual_history_id=history_id)
        return result

    async def resume(
        self,
        listing_id: int,
        *,
        run_id: int | None = None,
        options: ProcessingOptions | None = None,
    ) -> ProcessingResult:
        """Resume from the last committed stage; every stage is its own transaction."""

        opts = options or ProcessingOptions()
        decision: FilterDecision | None = None
        listing = await self._get_listing(listing_id)
        if listing.stage == ListingStage.DISCOVERED.value:
            try:
                enriched = await self._enrich(listing)
                await self._commit_enrichment(listing_id, enriched, run_id, opts)
            except BaseException as exc:
                await self._record_error(listing_id, run_id, "enrichment", exc)
                raise

        listing = await self._get_listing(listing_id)
        if listing.stage == ListingStage.ENRICHED.value:
            enriched = EnrichedListing(
                provider=listing.provider,
                url=listing.url,
                external_id=listing.external_id,
                data=dict(listing.data),
            )
            try:
                decision = await self.filters.evaluate(enriched)
                await self._commit_decision(listing_id, decision, run_id, opts)
            except BaseException as exc:
                await self._record_error(listing_id, run_id, "filtering", exc)
                raise


        listing = await self._get_listing(listing_id)
        stage = ListingStage(listing.stage)
        failed_channels: list[str] = []
        if stage is ListingStage.ACCEPTED and opts.deliver:
            await self._ensure_deliveries(
                listing_id, reset_skipped=opts.allow_skipped_delivery
            )
            failures: list[Exception] = []
            for channel, provider in self.outputs.items():
                if self._is_grouped_email(channel, provider):
                    continue
                try:
                    await self._deliver(listing_id, channel, provider)
                except BaseException as exc:
                    await self._record_error(
                        listing_id, run_id, f"delivery:{channel.value}", exc
                    )
                    if isinstance(exc, Exception):
                        failures.append(exc)
                        failed_channels.append(channel.value)
                    else:
                        raise
            if failures and opts.raise_errors:
                raise ExceptionGroup("one or more output deliveries failed", failures)
        return ProcessingResult(
            listing_id,
            stage,
            duplicate=False,
            decision=decision,
            data=dict(listing.data),
            delivery_failures=tuple(failed_channels),
        )

    async def _discover(
        self, raw: RawListing, *, run_id: int | None, options: ProcessingOptions
    ) -> tuple[int, bool]:
        natural_key = self.natural_key(raw)
        data = dict(raw.data)
        content_hash = self.content_hash(data)
        async with self.database.sessions.begin() as session:
            statement = (
                sqlite_insert(Listing)
                .values(
                    natural_key=natural_key,
                    provider=raw.provider,
                    external_id=raw.external_id,
                    url=raw.url,
                    stage=ListingStage.DISCOVERED.value,
                    data=data,
                    content_hash=content_hash,
                )
                .on_conflict_do_nothing(index_elements=[Listing.natural_key])
                .returning(Listing.id)
            )
            listing_id = (await session.execute(statement)).scalar_one_or_none()
            duplicate = listing_id is None
            if listing_id is None:
                listing_id = await session.scalar(
                    select(Listing.id).where(Listing.natural_key == natural_key)
                )
                assert listing_id is not None
            if options.record_watcher_history:
                await session.execute(
                    sqlite_insert(RunListing)
                    .values(
                        run_id=run_id,
                        listing_id=listing_id,
                        duplicate=duplicate,
                        final_stage=ListingStage.DISCOVERED.value,
                    )
                    .on_conflict_do_update(
                        index_elements=[RunListing.run_id, RunListing.listing_id],
                        set_={"duplicate": duplicate},
                    )
                ) if run_id is not None else None
                if not duplicate:
                    session.add(
                        ProcessingEvent(
                            listing_id=listing_id,
                            run_id=run_id,
                            from_stage=None,
                            to_stage=ListingStage.DISCOVERED.value,
                        )
                    )
        return listing_id, duplicate

    async def _enrich(self, listing: Listing) -> EnrichedListing:
        raw = RawListing(
            provider=listing.provider,
            url=listing.url,
            external_id=listing.external_id,
            data=dict(listing.data),
        )
        return await self._enrich_raw(raw, listing.content_hash)

    async def _enrich_raw(
        self,
        raw: RawListing,
        source_content_hash: str,
        *,
        use_cache: bool = True,
    ) -> EnrichedListing:
        if self.enricher is None:
            return EnrichedListing(raw.provider, raw.url, raw.external_id, raw.data)
        if not use_cache:
            return await self.enricher.enrich(raw)
        cache_namespace = str(
            getattr(self.enricher, "cache_namespace", self.enricher.name)
        )
        cache_key = hashlib.sha256(
            (
                f"{cache_namespace}\0{raw.provider.strip().lower()}\0"
                f"{raw.external_id or raw.url}\0{source_content_hash}"
            ).encode()
        ).hexdigest()
        now = utcnow()
        async with self.database.sessions() as session:
            cached = await session.scalar(
                select(EnrichmentCache).where(
                    EnrichmentCache.cache_key == cache_key,
                    (EnrichmentCache.expires_at.is_(None))
                    | (EnrichmentCache.expires_at > now),
                )
            )
        if cached is not None:
            value = cached.value
            return EnrichedListing(
                provider=value["provider"],
                url=value["url"],
                external_id=value.get("external_id"),
                data=value["data"],
            )
        enriched = await self.enricher.enrich(raw)
        value = {
            "provider": enriched.provider,
            "url": enriched.url,
            "external_id": enriched.external_id,
            "data": dict(enriched.data),
        }
        async with self.database.sessions.begin() as session:
            await session.execute(
                sqlite_insert(EnrichmentCache)
                .values(
                    cache_key=cache_key,
                    provider=cache_namespace,
                    value=value,
                    expires_at=now + self.enrichment_cache_ttl,
                )
                .on_conflict_do_update(
                    index_elements=[EnrichmentCache.cache_key],
                    set_={"value": value, "expires_at": now + self.enrichment_cache_ttl},
                )
            )
        return enriched

    async def _commit_enrichment(
        self,
        listing_id: int,
        enriched: EnrichedListing,
        run_id: int | None,
        options: ProcessingOptions,
    ) -> None:
        now = utcnow()
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(Listing)
                .where(
                    Listing.id == listing_id,
                    Listing.stage == ListingStage.DISCOVERED.value,
                )
                .values(
                    stage=ListingStage.ENRICHED.value,
                    provider=enriched.provider,
                    url=enriched.url,
                    external_id=enriched.external_id,
                    data=dict(enriched.data),
                    content_hash=self.content_hash(enriched.data),
                    enriched_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount and options.record_watcher_history:
                session.add(
                    ProcessingEvent(
                        listing_id=listing_id,
                        run_id=run_id,
                        from_stage=ListingStage.DISCOVERED.value,
                        to_stage=ListingStage.ENRICHED.value,
                    )
                )

    async def _commit_decision(
        self,
        listing_id: int,
        decision: FilterDecision,
        run_id: int | None,
        options: ProcessingOptions,
    ) -> None:
        stage = ListingStage.ACCEPTED if decision.accepted else ListingStage.REJECTED
        reasons = [
            {
                "code": reason.code,
                "message": reason.message,
                "source": reason.source.value,
                "rule": reason.rule,
                "details": dict(reason.details),
            }
            for reason in decision.reasons
        ]
        now = utcnow()
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(Listing)
                .where(
                    Listing.id == listing_id,
                    Listing.stage == ListingStage.ENRICHED.value,
                )
                .values(
                    stage=stage.value,
                    rejection_reasons=reasons,
                    decided_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount and options.record_watcher_history:
                session.add(
                    ProcessingEvent(
                        listing_id=listing_id,
                        run_id=run_id,
                        from_stage=ListingStage.ENRICHED.value,
                        to_stage=stage.value,
                        details={"rejection_reasons": reasons},
                    )
                )

    async def _ensure_deliveries(
        self, listing_id: int, *, reset_skipped: bool = False
    ) -> None:
        async with self.database.sessions.begin() as session:
            for channel, provider in self.outputs.items():
                if self._is_grouped_email(channel, provider):
                    continue
                await session.execute(
                    sqlite_insert(ListingDelivery)
                    .values(
                        listing_id=listing_id,
                        channel=channel.value,
                        state=DeliveryState.PENDING.value,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[ListingDelivery.listing_id, ListingDelivery.channel]
                    )
                )
                if reset_skipped:
                    await session.execute(
                        update(ListingDelivery)
                        .where(
                            ListingDelivery.listing_id == listing_id,
                            ListingDelivery.channel == channel.value,
                            ListingDelivery.state == DeliveryState.SKIPPED.value,
                        )
                        .values(
                            state=DeliveryState.PENDING.value,
                            last_error="operator explicitly requested delivery",
                            updated_at=utcnow(),
                        )
                    )

    async def _deliver(
        self,
        listing_id: int,
        channel: DeliveryChannel,
        provider: DeliveryProvider,
    ) -> None:
        async with self.database.sessions.begin() as session:
            delivery = await session.scalar(
                select(ListingDelivery).where(
                    ListingDelivery.listing_id == listing_id,
                    ListingDelivery.channel == channel.value,
                )
            )
            assert delivery is not None
            if delivery.state in {
                DeliveryState.SUCCEEDED.value,
                DeliveryState.SKIPPED.value,
                DeliveryState.IN_PROGRESS.value,
                DeliveryState.MANUAL_REVIEW.value,
            }:
                return
            attempt = await session.scalar(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery.id)
                .order_by(DeliveryAttempt.sequence.desc())
                .limit(1)
            )
            if attempt is None or attempt.state == DeliveryState.FAILED.value:
                sequence = 1 if attempt is None else attempt.sequence + 1
                attempt = DeliveryAttempt(
                    delivery_id=delivery.id,
                    sequence=sequence,
                    # Stable for the logical delivery, including known retries.
                    # Providers use this key to collapse an earlier success whose
                    # response was lost before local commit.
                    idempotency_key=f"qasawatch:{listing_id}:{channel.value}",
                    state=DeliveryState.IN_PROGRESS.value,
                )
                session.add(attempt)
            else:
                attempt.state = DeliveryState.IN_PROGRESS.value
                attempt.started_at = utcnow()
            delivery.state = DeliveryState.IN_PROGRESS.value
            delivery.last_error = None
            await session.flush()
            attempt_id = attempt.id
            key = attempt.idempotency_key

        snapshot = await self.snapshot(listing_id)
        try:
            result = await provider.deliver(snapshot, idempotency_key=key)
        except BaseException as exc:
            ambiguous = bool(getattr(exc, "ambiguous", False))
            async with self.database.sessions.begin() as session:
                attempt = await session.get(DeliveryAttempt, attempt_id)
                delivery = await session.get(ListingDelivery, attempt.delivery_id)  # type: ignore[union-attr]
                if attempt is not None:
                    attempt.state = (
                        DeliveryState.MANUAL_REVIEW.value
                        if ambiguous
                        else DeliveryState.FAILED.value
                    )
                    attempt.error = self._error_message(exc)
                    attempt.finished_at = utcnow()
                if delivery is not None:
                    delivery.state = (
                        DeliveryState.MANUAL_REVIEW.value
                        if ambiguous
                        else DeliveryState.FAILED.value
                    )
                    delivery.last_error = self._error_message(exc)
            raise
        async with self.database.sessions.begin() as session:
            attempt = await session.get(DeliveryAttempt, attempt_id)
            delivery = await session.get(ListingDelivery, attempt.delivery_id)  # type: ignore[union-attr]
            if attempt is not None:
                attempt.state = DeliveryState.SUCCEEDED.value
                attempt.provider_message_id = result.provider_message_id
                attempt.result = dict(result.details)
                attempt.finished_at = utcnow()
            if delivery is not None:
                delivery.state = DeliveryState.SUCCEEDED.value
                delivery.delivered_at = utcnow()

    async def resolve_delivery_manual_review(
        self,
        listing_id: int,
        channel: DeliveryChannel,
        *,
        delivered: bool,
    ) -> None:
        """Resolve a crash-ambiguous webhook/SMTP attempt explicitly."""

        async with self.database.sessions.begin() as session:
            delivery = await session.scalar(
                select(ListingDelivery).where(
                    ListingDelivery.listing_id == listing_id,
                    ListingDelivery.channel == channel.value,
                    ListingDelivery.state == DeliveryState.MANUAL_REVIEW.value,
                )
            )
            if delivery is None:
                raise RuntimeError("delivery is not awaiting manual review")
            delivery.state = (
                DeliveryState.SUCCEEDED.value
                if delivered
                else DeliveryState.FAILED.value
            )
            delivery.last_error = None if delivered else "operator confirmed not delivered"
            attempt = await session.scalar(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery.id)
                .order_by(DeliveryAttempt.sequence.desc())
                .limit(1)
            )
            if attempt is not None and attempt.state == DeliveryState.MANUAL_REVIEW.value:
                attempt.state = (
                    DeliveryState.SUCCEEDED.value
                    if delivered
                    else DeliveryState.FAILED.value
                )
                attempt.error = None if delivered else "operator confirmed not delivered"
                attempt.finished_at = utcnow()
            if delivered:
                delivery.delivered_at = utcnow()

    async def snapshot(self, listing_id: int) -> ListingSnapshot:
        listing = await self._get_listing(listing_id)
        return ListingSnapshot(
            id=listing.id,
            provider=listing.provider,
            url=listing.url,
            external_id=listing.external_id,
            stage=ListingStage(listing.stage),
            data=dict(listing.data),
            discovered_at=listing.discovered_at,
        )

    async def _get_listing(self, listing_id: int) -> Listing:
        async with self.database.sessions() as session:
            listing = await session.get(Listing, listing_id)
            if listing is None:
                raise LookupError(f"listing {listing_id} does not exist")
            session.expunge(listing)
            return listing

    async def _record_error(
        self,
        listing_id: int | None,
        run_id: int | None,
        operation: str,
        error: BaseException,
    ) -> None:
        async with self.database.sessions.begin() as session:
            session.add(
                ProcessingError(
                    listing_id=listing_id,
                    run_id=run_id,
                    operation=operation,
                    error_type=type(error).__name__,
                    message=self._error_message(error),
                )
            )

    async def _update_run_listing(
        self,
        run_id: int | None,
        listing_id: int,
        stage: ListingStage,
        *,
        options: ProcessingOptions,
    ) -> None:
        if run_id is None or not options.record_watcher_history:
            return
        async with self.database.sessions.begin() as session:
            await session.execute(
                update(RunListing)
                .where(RunListing.run_id == run_id, RunListing.listing_id == listing_id)
                .values(final_stage=stage.value)
            )

    async def _increment_stat(self, run_id: int, key: str) -> None:
        async with self.database.sessions.begin() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            stats = dict(run.stats)
            stats[key] = stats.get(key, 0) + 1
            run.stats = stats

    async def _finish_manual(
        self,
        history_id: int,
        *,
        result: ProcessingResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        async with self.database.sessions.begin() as session:
            history = await session.get(ManualProcessing, history_id)
            assert history is not None
            history.status = (RunStatus.FAILED if error else RunStatus.SUCCEEDED).value
            history.finished_at = utcnow()
            history.error = self._error_message(error) if error else None
            if result is not None:
                history.listing_id = result.listing_id
                history.result = {
                    "listing_id": result.listing_id,
                    "stage": result.stage.value,
                    "duplicate": result.duplicate,
                    "data": dict(result.data),
                    "accepted": result.decision.accepted if result.decision else None,
                    "rejection_reasons": [
                        {
                            "code": reason.code,
                            "message": reason.message,
                            "source": reason.source.value,
                            "rule": reason.rule,
                            "details": dict(reason.details),
                        }
                        for reason in (result.decision.reasons if result.decision else ())
                    ],
                }

    @staticmethod
    def natural_key(raw: RawListing) -> str:
        identity = raw.external_id.strip() if raw.external_id else raw.url.strip()
        return hashlib.sha256(f"{raw.provider.strip().lower()}\0{identity}".encode()).hexdigest()

    @staticmethod
    def content_hash(data: Mapping[str, Any]) -> str:
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _raw_dict(raw: RawListing) -> dict[str, Any]:
        return {
            "provider": raw.provider,
            "url": raw.url,
            "external_id": raw.external_id,
            "data": dict(raw.data),
        }

    @staticmethod
    def _error_message(error: BaseException | None) -> str:
        if error is None:
            return ""
        return str(error)[:4000] or type(error).__name__
