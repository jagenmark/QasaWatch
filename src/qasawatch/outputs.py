"""Independent Sheets and Discord delivery adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .domain import DeliveryChannel, DeliveryProvider, DeliveryResult, ListingSnapshot


class OutputError(RuntimeError):
    pass


class AmbiguousOutputError(OutputError):
    """A transport started sending but remote acceptance is unknowable."""

    ambiguous = True


def listing_summary(listing: ListingSnapshot) -> dict[str, str]:
    """Stable, presentation-ready fields shared by all output channels."""

    data = listing.data
    commute_values = data.get("commutes") or ({"destination": data["commute"]} if isinstance(data.get("commute"), Mapping) else {})
    commute = "; ".join(
        f"{name}: {round(value['duration_seconds'] / 60)} min" if isinstance(value, Mapping) and value.get("duration_seconds") is not None else f"{name}: {value.get('status', 'unknown') if isinstance(value, Mapping) else 'unknown'}"
        for name, value in commute_values.items()
    )
    demographics = data.get("demographics") or data.get("scb") or {}
    demographics_text = ", ".join(f"{key}: {value}" for key, value in demographics.items() if value not in (None, "")) if isinstance(demographics, Mapping) else str(demographics)
    filter_value = data.get("filter_result", data.get("filter", "accepted" if listing.stage.value == "accepted" else listing.stage.value))
    if isinstance(filter_value, Mapping):
        filter_value = filter_value.get("status", filter_value.get("accepted", filter_value))
    return {
        "title": str(data.get("title") or data.get("address") or f"Listing {listing.id}"),
        "address": str(data.get("address") or ""),
        "rent": str(data.get("rent", data.get("monthly_rent", ""))),
        "rooms": str(data.get("rooms", "")),
        "area": str(data.get("area", "")),
        "coordinates": ", ".join(
            str(data.get(key)) for key in ("latitude", "longitude")
            if data.get(key) is not None
        ),
        "rental_period": " → ".join(
            str(data.get(key)) for key in ("rental_start", "rental_end")
            if data.get(key)
        ),
        "duration": str(data.get("duration", "")),
        "availability": str(data.get("availability", "")),
        "published": str(data.get("published_at", data.get("published", ""))),
        "discovered": listing.discovered_at.isoformat(),
        "commute": commute,
        "demographics": demographics_text,
        "filter": str(filter_value),
        "url": listing.url,
    }


def output_idempotency_key(listing: ListingSnapshot | int | str, channel: DeliveryChannel | str, *, event: str = "accepted", version: int = 1) -> str:
    """Stable across restarts and retry attempts for one logical delivery."""

    identity = listing.id if isinstance(listing, ListingSnapshot) else listing
    channel_value = channel.value if isinstance(channel, DeliveryChannel) else str(channel)
    canonical = f"qasawatch\0{identity}\0{channel_value}\0{event}\0v{version}"
    return "qw_" + hashlib.sha256(canonical.encode()).hexdigest()


def grouped_idempotency_key(listing_ids: Iterable[int], channel: DeliveryChannel | str, *, scan_id: str | int) -> str:
    ids = ",".join(str(item) for item in sorted(set(listing_ids)))
    channel_value = channel.value if isinstance(channel, DeliveryChannel) else str(channel)
    return "qw_" + hashlib.sha256(f"qasawatch\0scan:{scan_id}\0{ids}\0{channel_value}".encode()).hexdigest()


class SheetsClient(Protocol):
    async def contains_idempotency_key(self, spreadsheet_id: str, worksheet: str, key: str) -> bool: ...
    async def append_row(self, spreadsheet_id: str, worksheet: str, values: list[Any]) -> Any: ...


class GoogleServiceAccountSheetsClient:
    """Minimal Google Sheets v4 client using service-account credentials.

    ``credential_material`` may be the JSON value itself or an absolute path to
    a JSON key file supplied through an environment-backed secret reference.
    The credential is never included in reprs or raised error messages.
    """

    _SCOPE = "https://www.googleapis.com/auth/spreadsheets"

    def __init__(self, credential_material: str, *, timeout: float = 20.0) -> None:
        if not credential_material.strip():
            raise ValueError("Google Sheets credentials are required")
        try:
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError("install google-auth to use Google Sheets") from exc
        stripped = credential_material.strip()
        try:
            if stripped.startswith("{"):
                info = json.loads(stripped)
                self._credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=[self._SCOPE]
                )
            else:
                self._credentials = service_account.Credentials.from_service_account_file(
                    str(Path(stripped).expanduser()), scopes=[self._SCOPE]
                )
        except Exception as exc:
            raise ValueError("Google Sheets service-account credentials are invalid") from exc
        self.timeout = timeout
        self._refresh_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(credentials=<redacted>)"

    async def contains_idempotency_key(
        self, spreadsheet_id: str, worksheet: str, key: str
    ) -> bool:
        column = await self._request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{quote(worksheet + '!A:A', safe='')}",
            query={"majorDimension": "COLUMNS"},
        )
        values = column.get("values", []) if isinstance(column, Mapping) else []
        return bool(values and key in values[0])

    async def append_row(
        self, spreadsheet_id: str, worksheet: str, values: list[Any]
    ) -> Any:
        result = await self._request(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{quote(worksheet + '!A:O', safe='')}:append",
            query={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            body={"majorDimension": "ROWS", "values": [values]},
        )
        return result.get("updates", {}).get("updatedRange") if isinstance(result, Mapping) else None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        async with self._refresh_lock:
            if not self._credentials.valid:
                try:
                    from google.auth.transport.requests import Request as AuthRequest
                    await asyncio.to_thread(self._credentials.refresh, AuthRequest())
                except Exception as exc:
                    raise OutputError("Google Sheets authentication failed") from exc
            token = self._credentials.token
        if query:
            url += "?" + urlencode(query)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)

        def send() -> Any:
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read() or b"{}")
            except HTTPError as exc:
                raise OutputError(f"Google Sheets request failed (HTTP {exc.code})") from exc

        return await asyncio.to_thread(send)


class GoogleSheetsOutput:
    channel = DeliveryChannel.SHEETS

    def __init__(self, client: SheetsClient, spreadsheet_id: str, *, worksheet: str = "Listings") -> None:
        if not spreadsheet_id.strip() or not worksheet.strip():
            raise ValueError("spreadsheet_id and worksheet are required")
        self.client, self.spreadsheet_id, self.worksheet = client, spreadsheet_id, worksheet

    async def deliver(self, listing: ListingSnapshot, *, idempotency_key: str) -> DeliveryResult:
        # The key is a dedicated stable column. A developer-metadata backed client
        # may implement the same two operations without scanning cell values.
        try:
            if await self.client.contains_idempotency_key(self.spreadsheet_id, self.worksheet, idempotency_key):
                return DeliveryResult(idempotency_key, {"duplicate": True})
            summary = listing_summary(listing)
            values = [
                idempotency_key, listing.external_id or "", summary["url"],
                summary["discovered"], summary["address"], summary["rent"],
                summary["rooms"], summary["area"], summary["coordinates"],
                summary["rental_period"], summary["duration"], summary["availability"],
                summary["commute"], summary["demographics"], summary["filter"],
            ]
            response = await self.client.append_row(self.spreadsheet_id, self.worksheet, values)
        except Exception as exc:
            raise OutputError(f"Google Sheets delivery failed ({type(exc).__name__})") from exc
        return DeliveryResult(str(response) if response is not None else None, {"duplicate": False})


class WebhookClient(Protocol):
    async def post(self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str] | None = None) -> Any: ...


class DiscordWebhookOutput:
    channel = DeliveryChannel.DISCORD

    def __init__(self, webhook_url: str, client: WebhookClient) -> None:
        if not webhook_url.startswith("https://"):
            raise ValueError("Discord webhook URL must use HTTPS")
        self._webhook_url, self.client = webhook_url, client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(webhook_url=<redacted>)"

    async def deliver(self, listing: ListingSnapshot, *, idempotency_key: str) -> DeliveryResult:
        summary = listing_summary(listing)
        content = f"**{summary['title'][:120]}**"
        if summary["rent"]:
            content += f" — {summary['rent']}"
        details = [value for value in (summary["published"] and f"Published {summary['published']}", summary["commute"], summary["demographics"], f"Filter: {summary['filter']}") if value]
        if details:
            content += "\n" + " · ".join(details)
        content += f"\n{summary['url']}"
        payload = {"content": content[:1900], "allowed_mentions": {"parse": []}}
        try:
            response = await self.client.post(self._webhook_url, payload, headers={"Idempotency-Key": idempotency_key})
        except Exception as exc:
            if getattr(exc, "ambiguous", False):
                raise
            raise OutputError(f"Discord delivery failed ({type(exc).__name__})") from exc
        message_id = response.get("id") if isinstance(response, Mapping) else None
        return DeliveryResult(str(message_id) if message_id else None)


@dataclass(frozen=True, slots=True)
class IndependentDeliveryOutcome:
    channel: DeliveryChannel
    result: DeliveryResult | None = None
    error: Exception | None = None


async def deliver_independently(providers: Iterable[DeliveryProvider], listing: ListingSnapshot) -> tuple[IndependentDeliveryOutcome, ...]:
    """Run outputs sequentially but isolate each provider's exception."""

    outcomes: list[IndependentDeliveryOutcome] = []
    for provider in providers:
        key = output_idempotency_key(listing, provider.channel)
        try:
            result = await provider.deliver(listing, idempotency_key=key)
        except Exception as exc:
            outcomes.append(IndependentDeliveryOutcome(provider.channel, error=exc))
        else:
            outcomes.append(IndependentDeliveryOutcome(provider.channel, result=result))
    return tuple(outcomes)

# Short aliases for common configuration code.
GoogleSheetsProvider = GoogleSheetsOutput
DiscordWebhookProvider = DiscordWebhookOutput
