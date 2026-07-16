"""Serialized Playwright-over-CDP jobs against the supervised Chrome host."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .browser_host import BrowserDescriptor, BrowserHostError, ChromeHost
from .domain import EnrichedListing, RawListing
from .parser import ParsedPage, latest_home_search_page, parse_qasa_html
from .readiness import PageSample, ReadinessResult, ReadinessState, classify_samples

T = TypeVar("T")


def validate_qasa_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not (hostname == "qasa.com" or hostname.endswith(".qasa.com")):
        raise ValueError("URL must use HTTPS on qasa.com or a qasa.com subdomain")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    return url


@dataclass(frozen=True, slots=True)
class BrowserScan:
    parsed: ParsedPage
    readiness: ReadinessResult
    final_url: str
    pages_scanned: int = 1
    total_available: int | None = None
    truncated: bool = False


class QasaBrowser:
    def __init__(self, host: ChromeHost, *, sample_interval: float = 0.4, stable_samples: int = 2) -> None:
        self.host = host
        self.sample_interval = sample_interval
        self.stable_samples = stable_samples
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._descriptor: BrowserDescriptor | None = None

    async def start_host(self) -> BrowserDescriptor:
        """Start/adopt real Chrome without delaying UI startup on automation."""

        descriptor = await asyncio.to_thread(self.host.start_or_adopt)
        self._descriptor = descriptor
        return descriptor

    async def connect(self) -> None:
        descriptor = self._descriptor
        if descriptor is None or not await asyncio.to_thread(
            self.host.descriptor_healthy, descriptor
        ):
            descriptor = await self.start_host()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserHostError("Playwright is required for browser automation; install qasawatch[browser]") from exc
        self._playwright = await async_playwright().start()
        try:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{descriptor.port}")
            except Exception:
                descriptor = await asyncio.to_thread(self.host.recover)
                self._browser = await self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{descriptor.port}")
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            self._browser = None
            raise
        self._descriptor = descriptor

    async def host_running(self) -> bool:
        return await asyncio.to_thread(
            self.host.descriptor_healthy, self._descriptor
        )

    async def close(self) -> None:
        # Stopping Playwright drops its transport. Do not call Browser.close(),
        # which could send a shutdown command to the externally supervised Chrome.
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None
        self.host.close()

    async def job(self, url: str, operation: Callable[[Any], Awaitable[T]]) -> T:
        validate_qasa_url(url)
        async with self._lock:
            if not self._browser or not self._browser.is_connected():
                await self.connect()
            contexts = self._browser.contexts
            context = contexts[0] if contexts else await self._browser.new_context()
            page = await context.new_page()
            tag = f"qasawatch:{uuid.uuid4().hex}"
            captured_json: list[Any] = []

            async def capture(response: Any) -> None:
                try:
                    validate_qasa_url(response.url)
                    headers = response.headers
                    content_type = headers.get("content-type", "").lower()
                    length = int(headers.get("content-length", "0") or 0)
                    if "json" in content_type and length <= 5_000_000 and len(captured_json) < 50:
                        payload = await response.json()
                        operation_name = None
                        try:
                            request_body = json.loads(response.request.post_data or "{}")
                            operation_name = request_body.get("operationName")
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            pass
                        captured_json.append(
                            {
                                "__qasawatch_operation": operation_name,
                                "payload": payload,
                            }
                        )
                except (ValueError, TypeError, OSError):
                    return

            try:
                await page.add_init_script(f"window.name = {tag!r}")
                page.on("response", capture)
                await page.goto(url, wait_until="domcontentloaded")
                validate_qasa_url(page.url)  # reject an off-site final redirect
                setattr(page, "_qasawatch_captured_json", captured_json)
                return await operation(page)
            finally:
                await page.close()

    async def scan(
        self,
        url: str,
        *,
        timeout: float = 20.0,
        results_only: bool = False,
        known_listing_ids: Collection[str] = (),
        max_pages: int = 1,
        max_listings: int | None = None,
    ) -> BrowserScan:
        async def operation(page: Any) -> BrowserScan:
            async def wait_until_stable(
                *, minimum_search_responses: int = 0
            ) -> tuple[ParsedPage, ReadinessResult]:
                deadline = time.monotonic() + timeout
                samples: list[PageSample] = []
                latest: ParsedPage | None = None
                result = classify_samples(())
                while time.monotonic() < deadline:
                    captured = getattr(page, "_qasawatch_captured_json", ())
                    if (
                        results_only
                        and _home_search_response_count(captured)
                        < minimum_search_responses
                    ):
                        await asyncio.sleep(self.sample_interval)
                        continue
                    latest = parse_qasa_html(
                        await page.content(),
                        base_url=page.url,
                        captured_json=captured,
                        results_only=results_only,
                    )
                    keys = tuple(
                        sorted(item.external_id or item.url for item in latest.listings)
                    )
                    samples.append(
                        PageSample(
                            page.url,
                            keys,
                            latest.explicit_empty,
                            latest.loading,
                            latest.auth_required,
                            latest.captcha,
                            latest.errors[0] if latest.errors and not keys else None,
                        )
                    )
                    result = classify_samples(
                        samples, stable_samples=self.stable_samples
                    )
                    if result.complete or result.state in (
                        ReadinessState.AUTH_REQUIRED,
                        ReadinessState.CAPTCHA,
                        ReadinessState.ERROR,
                    ):
                        return latest, result
                    await asyncio.sleep(self.sample_interval)
                return latest or ParsedPage(()), result

            latest, result = await wait_until_stable()
            if not results_only or not result.complete:
                return BrowserScan(latest, result, page.url)

            known = {str(value) for value in known_listing_ids}
            page_info = latest_home_search_page(
                getattr(page, "_qasawatch_captured_json", ())
            )
            pages_scanned = 1
            total_available = page_info.total_count if page_info else None
            capped_pages = max(1, max_pages)
            truncated = False

            while page_info is not None and page_info.has_next_page:
                if page_info.listing_ids and all(
                    listing_id in known for listing_id in page_info.listing_ids
                ):
                    break
                if pages_scanned >= capped_pages:
                    truncated = True
                    break
                if max_listings is not None and len(latest.listings) >= max_listings:
                    truncated = True
                    break
                next_page = _page_number(page.url) + 1
                expected_responses = (
                    _home_search_response_count(
                        getattr(page, "_qasawatch_captured_json", ())
                    )
                    + 1
                )
                await page.goto(
                    _results_page_url(page.url, next_page),
                    wait_until="domcontentloaded",
                )
                validate_qasa_url(page.url)
                latest, result = await wait_until_stable(
                    minimum_search_responses=expected_responses
                )
                pages_scanned += 1
                if not result.complete:
                    return BrowserScan(
                        latest,
                        result,
                        page.url,
                        pages_scanned,
                        total_available,
                        truncated,
                    )
                page_info = latest_home_search_page(
                    getattr(page, "_qasawatch_captured_json", ())
                )
                if page_info and page_info.total_count is not None:
                    total_available = page_info.total_count

            if max_listings is not None and len(latest.listings) > max_listings:
                latest = ParsedPage(
                    latest.listings[:max_listings],
                    latest.explicit_empty,
                    latest.loading,
                    latest.auth_required,
                    latest.captcha,
                    latest.errors,
                )
                truncated = True
                keys = tuple(
                    sorted(item.external_id or item.url for item in latest.listings)
                )
                result = ReadinessResult(
                    ReadinessState.READY, "stable paginated listing results", keys
                )
            return BrowserScan(
                latest,
                result,
                page.url,
                pages_scanned,
                total_available,
                truncated,
            )
        return await self.job(url, operation)


class QasaDetailEnricher:
    """Hydrate a newly discovered result through its rendered detail page.

    This provider belongs first in an enrichment chain. Existing result-page
    values are retained when a detail page omits them, while richer detail-page
    values and field provenance are merged into the same listing payload.
    """

    name = "qasa-detail"

    def __init__(self, browser: QasaBrowser, *, timeout: float = 30.0) -> None:
        self.browser = browser
        self.timeout = timeout

    async def enrich(self, listing: RawListing) -> EnrichedListing:
        if listing.data.get("detail_page_rendered") is True:
            return EnrichedListing(
                listing.provider, listing.url, listing.external_id, dict(listing.data)
            )
        scan = await self.browser.scan(listing.url, timeout=self.timeout)
        if not scan.readiness.complete:
            raise BrowserHostError(
                f"Qasa detail page was not complete: {scan.readiness.state.value}"
            )
        candidates = [
            item
            for item in scan.parsed.listings
            if not listing.external_id or item.external_id == listing.external_id
        ]
        if not candidates:
            raise BrowserHostError("Qasa listing is unavailable or no longer exists")

        detail = candidates[0].to_raw_listing()
        data = dict(listing.data)
        prior_provenance = dict(data.get("provenance", {}))
        detail_data = dict(detail.data)
        detail_provenance = dict(detail_data.pop("provenance", {}))
        partial_errors = list(data.get("partial_errors", ()))
        partial_errors.extend(detail_data.pop("partial_errors", ()))
        for key, value in detail_data.items():
            if value not in (None, "", [], {}):
                data[key] = value
        data["provenance"] = {**prior_provenance, **detail_provenance}
        if partial_errors:
            data["partial_errors"] = list(dict.fromkeys(map(str, partial_errors)))
        data["detail_page_rendered"] = True
        return EnrichedListing(
            provider=listing.provider,
            url=detail.url,
            external_id=detail.external_id or listing.external_id,
            data=data,
        )


def _page_number(url: str) -> int:
    try:
        query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
        return max(
            1,
            int(query.get("page", "1")),
        )
    except ValueError:
        return 1


def _results_page_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "page"
    ]
    if page > 1:
        query.append(("page", str(page)))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _home_search_response_count(captured_json: Collection[Any]) -> int:
    return sum(
        1
        for captured in captured_json
        if isinstance(captured, dict)
        and captured.get("__qasawatch_operation") == "HomeSearch"
    )
