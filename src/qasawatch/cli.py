"""Command-line entry point for the local operator application."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .app import create_app
from .browser import QasaBrowser
from .browser_host import ChromeHost
from .config import BootstrapSettings, ConfigStore, load_env_file
from .db import Database
from .pipeline import Pipeline
from .domain import DeliveryChannel, EnrichedListing, RawListing
from .emailer import EmailMode, EmailOutput, SMTPConfig, SMTPProvider
from .enrichment import (
    CommuteEnricher,
    GeocodingEnricher,
    GoogleGeocoder,
    GoogleRoutesMatrix,
)
from .filters import FilterChain, NumericRangeFilter, PredicateFilter
from .outputs import (
    DiscordWebhookOutput,
    AmbiguousOutputError,
    GoogleServiceAccountSheetsClient,
    GoogleSheetsOutput,
    OutputError,
)
from .scb import FieldMapping, GeoJSONSCBDataset, OptionalSCBDataset, SCBEnricher
from .secrets import EnvironmentSecretResolver, SecretRef
from .service import AppService


class _CompositeEnricher:
    name = "qasa-detail-and-commute"
    def __init__(self, enrichers, *, cache_namespace):
        self.enrichers = tuple(enrichers)
        self.cache_namespace = cache_namespace
    async def enrich(self, listing: RawListing) -> EnrichedListing:
        current = listing
        for enricher in self.enrichers:
            value = await enricher.enrich(current)
            data = dict(value.data)
            if current.data.get("commutes") and data.get("commutes"):
                data["commutes"] = {**current.data["commutes"], **data["commutes"]}
            current = RawListing(value.provider, value.url, value.external_id, data)
        return EnrichedListing(current.provider, current.url, current.external_id, current.data)


class _WebhookClient:
    async def post(self, url, payload, *, headers=None):
        def send():
            url_with_wait = url + ("&" if "?" in url else "?") + "wait=true"
            request = Request(
                url_with_wait,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "QasaWatch/2.0 (+https://github.com/jagenmark/QasaWatch)",
                    **(headers or {}),
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=20) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except HTTPError as exc:
                raise OutputError(f"Discord webhook failed (HTTP {exc.code})") from exc
            except Exception as exc:
                raise AmbiguousOutputError(
                    f"Discord webhook outcome is ambiguous ({type(exc).__name__})"
                ) from exc
        return await asyncio.to_thread(send)


def _resolve(reference: str | None) -> str | None:
    return EnvironmentSecretResolver().resolve(SecretRef.parse(reference)) if reference else None


def _filters(settings, destinations=()) -> FilterChain:
    rules = []
    for field, minimum, maximum in (("rent", settings.minimum_rent, settings.maximum_rent), ("rooms", settings.minimum_rooms, settings.maximum_rooms), ("area", settings.minimum_area, settings.maximum_area)):
        if minimum is not None or maximum is not None:
            rules.append(NumericRangeFilter(field, minimum, maximum, name=f"configured_{field}"))
    def text(listing): return " ".join(str(value) for value in listing.data.values()).lower()
    if settings.required_keywords:
        rules.append(PredicateFilter("required_keywords", lambda item: all(word.lower() in text(item) for word in settings.required_keywords), "keywords.required_missing", "one or more required keywords are missing"))
    if settings.excluded_keywords:
        rules.append(PredicateFilter("excluded_keywords", lambda item: not any(word.lower() in text(item) for word in settings.excluded_keywords), "keywords.excluded", "an excluded keyword matched"))
    if settings.allowed_locations:
        rules.append(PredicateFilter("allowed_locations", lambda item: any(place.lower() in str(item.data.get("address", "")).lower() for place in settings.allowed_locations), "location.not_allowed", "listing is outside allowed locations"))
    if settings.excluded_locations:
        rules.append(PredicateFilter("excluded_locations", lambda item: not any(place.lower() in str(item.data.get("address", "")).lower() for place in settings.excluded_locations), "location.excluded", "listing is in an excluded location"))
    if settings.availability_from:
        rules.append(PredicateFilter("availability_from", lambda item: str(item.data.get("rental_start", "")) >= settings.availability_from, "availability.too_early", "listing starts before the configured date"))
    if settings.availability_to:
        rules.append(PredicateFilter("availability_to", lambda item: str(item.data.get("rental_start", "")) <= settings.availability_to, "availability.too_late", "listing starts after the configured date"))
    for attribute, expected in settings.attribute_requirements.items():
        def attribute_matches(item, attribute=attribute, expected=expected):
            value = item.data.get(attribute)
            return isinstance(value, bool) and value is expected

        expectation = "yes" if expected else "no"
        rules.append(
            PredicateFilter(
                f"attribute_{attribute}",
                attribute_matches,
                f"attribute.{attribute}.required_{str(expected).lower()}",
                f"{attribute.replace('_', ' ')} must be {expectation}; the value is missing or does not match",
            )
        )
    for destination in destinations:
        if destination.maximum_commute_minutes is not None:
            rules.append(PredicateFilter(f"commute_{destination.label}", lambda item, destination=destination: item.data.get("commutes", {}).get(destination.label, {}).get("duration_seconds") is not None and item.data["commutes"][destination.label]["duration_seconds"] <= destination.maximum_commute_minutes * 60, "commute.destination_limit", f"commute to {destination.label} exceeds its limit or is unavailable"))
    for field, minimum, maximum in (
        ("population", settings.minimum_population, settings.maximum_population),
        ("average_age", None, settings.maximum_average_age),
        (
            "foreign_background_percent",
            settings.minimum_foreign_background_percent,
            settings.maximum_foreign_background_percent,
        ),
    ):
        if minimum is not None or maximum is not None:
            def demographic_ok(item, field=field, minimum=minimum, maximum=maximum):
                value = item.data.get("demographics", {}).get(field)
                try:
                    number = float(value)
                except (TypeError, ValueError, AttributeError):
                    return False
                return (minimum is None or number >= minimum) and (
                    maximum is None or number <= maximum
                )
            rules.append(
                PredicateFilter(
                    f"demographic_{field}", demographic_ok,
                    f"demographic.{field}_outside_range",
                    f"demographic field {field} is missing or outside its configured range",
                )
            )
    return FilterChain(rules)


def build_application(database_path: str | None = None):
    load_env_file()
    settings = BootstrapSettings.from_env()
    database = Database(database_path or settings.database)
    state_dir = Path(database_path or settings.database).expanduser().resolve().parent / ".qasawatch"
    browser = QasaBrowser(ChromeHost(state_dir))
    dataset_cache: dict[tuple, object] = {}
    maps_client_cache: dict[str, tuple[GoogleGeocoder, GoogleRoutesMatrix]] = {}
    commute_cache: dict[tuple, CommuteEnricher] = {}
    def pipeline_factory(config):
        # The rendered results page's HomeSearch data is the watcher's source
        # of truth. Manual inspection separately opens one submitted detail URL;
        # watcher listings must not fan out into one browser navigation each.
        enrichers = []
        enrichment_config = {
            "version": 2,
            "destinations": [item.model_dump(mode="json") for item in config.destinations],
            "maps_reference": config.maps_api_secret_ref,
            "scb": config.scb.model_dump(mode="json"),
        }
        maps_needed = bool(config.destinations or config.scb.data_path)
        geocoder = routes = None
        if config.maps_api_secret_ref and maps_needed:
            clients = maps_client_cache.get(config.maps_api_secret_ref)
            if clients is None:
                key = _resolve(config.maps_api_secret_ref)
                clients = GoogleGeocoder(key), GoogleRoutesMatrix(key)
                maps_client_cache[config.maps_api_secret_ref] = clients
            geocoder, routes = clients
        if geocoder is not None and config.scb.data_path and not config.destinations:
            enrichers.append(GeocodingEnricher(geocoder))
        if geocoder is not None and routes is not None and config.destinations:
            for time_kind in ("arrival", "departure"):
                destinations = {
                    item.label: item.address
                    for item in config.destinations
                    if item.commute_mode == time_kind
                }
                if destinations:
                    cache_key = (
                        config.maps_api_secret_ref,
                        time_kind,
                        tuple(sorted(destinations.items())),
                    )
                    commute = commute_cache.get(cache_key)
                    if commute is None:
                        commute = CommuteEnricher(
                            geocoder, routes, destinations, time_kind=time_kind
                        )
                        commute_cache[cache_key] = commute
                    enrichers.append(commute)
        if config.scb.data_path:
            try:
                stat = Path(config.scb.data_path).expanduser().stat()
                dataset_file = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                dataset_file = (None, None)
            enrichment_config["scb_file"] = dataset_file
            cache_key = (
                config.scb.data_path, config.scb.id_column, config.scb.name_column,
                tuple(sorted(config.scb.demographic_mapping.items())), config.scb.vintage,
                dataset_file,
            )
            dataset = dataset_cache.get(cache_key)
            if dataset is None:
                try:
                    dataset = GeoJSONSCBDataset.from_file(
                        config.scb.data_path,
                        fields=FieldMapping(
                            id_field=config.scb.id_column,
                            name_field=config.scb.name_column,
                            demographic_fields=config.scb.demographic_mapping,
                        ),
                        expected_vintage=config.scb.vintage or None,
                    )
                    if dataset.metadata.crs != config.scb.crs.upper():
                        raise ValueError(
                            "SCB dataset CRS does not match configured CRS"
                        )
                except Exception as exc:
                    dataset = OptionalSCBDataset(
                        None,
                        diagnostic=f"SCB dataset could not be loaded ({type(exc).__name__})",
                    )
                dataset_cache[cache_key] = dataset
            enrichers.append(
                SCBEnricher(
                    dataset if isinstance(dataset, OptionalSCBDataset)
                    else OptionalSCBDataset(dataset)
                )
            )
        outputs = []
        if not config.safe_mode and config.discord.enabled:
            if not config.discord.webhook_secret_ref: raise RuntimeError("Discord is enabled but webhook_secret_ref is missing")
            outputs.append(DiscordWebhookOutput(_resolve(config.discord.webhook_secret_ref), _WebhookClient()))
        if not config.safe_mode and config.email.enabled:
            if not config.email.smtp_secret_ref: raise RuntimeError("Email is enabled but smtp_secret_ref is missing")
            password = _resolve(config.email.smtp_secret_ref)
            smtp = SMTPProvider(SMTPConfig(config.email.smtp_host, config.email.smtp_port, config.email.sender, username=config.email.smtp_username or config.email.sender, password=password, starttls=config.email.smtp_mode == "starttls", use_ssl=config.email.smtp_mode == "tls"))
            outputs.append(EmailOutput(smtp, config.email.recipients, mode=EmailMode.PER_SCAN if config.email.grouped else EmailMode.PER_LISTING, send_if_empty=config.email.send_no_new, subject_template=config.email.subject))
        if not config.safe_mode and config.sheets.enabled:
            if not config.sheets.credentials_secret_ref:
                raise RuntimeError(
                    "Google Sheets is enabled but credentials_secret_ref is missing"
                )
            credentials = _resolve(config.sheets.credentials_secret_ref)
            outputs.append(
                GoogleSheetsOutput(
                    GoogleServiceAccountSheetsClient(credentials),
                    config.sheets.spreadsheet_id,
                    worksheet=config.sheets.worksheet,
                )
            )
        namespace = "qasa-pipeline-v1:" + hashlib.sha256(
            json.dumps(enrichment_config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return Pipeline(database, enricher=_CompositeEnricher(enrichers, cache_namespace=namespace), filters=_filters(config.filters, config.destinations), outputs=outputs)
    store = ConfigStore(database)
    async def email_tester(recipient):
        config = await service.get_config()
        provider = pipeline_factory(config).outputs.get(DeliveryChannel.EMAIL)
        if provider is None: raise RuntimeError("email provider is not configured")
        if recipient and recipient not in provider.recipients:
            provider = EmailOutput(
                provider.sender,
                [recipient],
                mode=provider.mode,
                subject_template=provider.subject_template,
            )
        return dict((await provider.send_test()).details)
    async def discord_tester():
        config = await service.get_config()
        if not config.discord.webhook_secret_ref:
            raise RuntimeError("Discord webhook is not configured")
        response = await _WebhookClient().post(
            _resolve(config.discord.webhook_secret_ref),
            {
                "content": "QasaWatch test: Discord notifications are connected.",
                "allowed_mentions": {"parse": []},
            },
        )
        return {
            "message_id": response.get("id")
            if isinstance(response, dict)
            else None
        }
    async def maps_tester():
        config = await service.get_config()
        key = _resolve(config.maps_api_secret_ref)
        if not key:
            raise RuntimeError("Google Maps credentials are not available")
        geocoder = GoogleGeocoder(key)
        origin = await geocoder.geocode(
            "Stockholm Centralstation, Stockholm, Sweden"
        )
        if origin.status.value != "ok" or origin.coordinates is None:
            raise RuntimeError(
                f"Google Maps geocoding test failed ({origin.status.value})"
            )
        result: dict[str, object] = {
            "geocoding": "ok",
            "routes": "not needed",
        }
        if config.destinations:
            destination_config = config.destinations[0]
            destination = await geocoder.geocode(destination_config.address)
            if destination.status.value != "ok" or destination.coordinates is None:
                raise RuntimeError(
                    f"Google Maps destination geocoding failed ({destination.status.value})"
                )
            route_kwargs = (
                {"arrival_time": datetime.now(UTC) + timedelta(hours=1)}
                if destination_config.commute_mode == "arrival"
                else {"departure_time": datetime.now(UTC) + timedelta(minutes=5)}
            )
            route = await GoogleRoutesMatrix(key).compute_route(
                origin.coordinates,
                destination.coordinates,
                travel_mode="TRANSIT",
                **route_kwargs,
            )
            if route.status.value != "ok":
                raise RuntimeError(
                    f"Google Maps routes test failed ({route.status.value})"
                )
            result["routes"] = "ok"
            result["duration_seconds"] = route.duration_seconds
        return result
    async def sheets_tester():
        config = await service.get_config()
        credentials = _resolve(config.sheets.credentials_secret_ref)
        if not credentials:
            raise RuntimeError("Google Sheets credentials are not available")
        client = GoogleServiceAccountSheetsClient(credentials)
        return await client.verify_connection(
            config.sheets.spreadsheet_id,
            config.sheets.worksheet,
        )
    service = AppService(
        database,
        browser,
        Pipeline(database),
        config_store=store,
        pipeline_factory=pipeline_factory,
        email_tester=email_tester,
        discord_tester=discord_tester,
        maps_tester=maps_tester,
        sheets_tester=sheets_tester,
    )
    return create_app(service, start_scheduler=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the QasaWatch operator dashboard")
    parser.add_argument("--database")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(build_application(args.database), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
