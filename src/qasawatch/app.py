"""FastAPI operator dashboard and JSON API."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .browser_host import BrowserHostError
from .scheduler import WatchScheduler
from .schemas import ManualRequest, PromotionRequest, RetryRequest, TestEmailRequest, WatcherConfig
from .service import AppService, IncompletePageError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def _json_object(value: str, label: str) -> dict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} JSON must contain an object")
    return parsed


def create_app(service: AppService, *, start_scheduler: bool = False) -> FastAPI:
    if service.scheduler is None:
        service.scheduler = WatchScheduler(service.database, service.config_store, service.run_watcher)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.database.initialize()
        await service.database.recover_interrupted_work()
        start_browser = getattr(service.browser, "start_host", None) or getattr(
            service.browser, "connect", None
        )
        if start_browser is not None:
            try:
                await start_browser()
                service.last_browser_state = {"status": "running", "errors": []}
            except Exception as exc:
                # Keep the operator interface available for diagnosis/config.
                message = (
                    str(exc)
                    if isinstance(exc, BrowserHostError)
                    else f"browser startup failed ({type(exc).__name__})"
                )
                service.last_browser_state = {
                    "status": "error",
                    "errors": [message],
                }
        if start_scheduler:
            await service.scheduler.start()
        try:
            yield
        finally:
            await service.scheduler.stop()
            close = getattr(service.browser, "close", None)
            if close is not None:
                await close()
            await service.database.dispose()

    app = FastAPI(title="QasaWatch", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        state = await service.dashboard()
        state["config_error"] = request.query_params.get("config_error")
        return TEMPLATES.TemplateResponse(request, "dashboard.html", state)

    @app.get("/api/status")
    async def api_status():
        state = await service.dashboard()
        return {
            "watcher": state["watcher"], "browser": state["browser"],
            "connections": state["connections"],
            "email": state["email"], "recent_runs": [_run_json(item) for item in state["runs"]],
        }

    @app.get("/api/config")
    async def get_config():
        return await service.public_config()

    @app.put("/api/config")
    async def put_config(config: WatcherConfig):
        await service.save_config(config)
        return await service.public_config()

    @app.post("/config")
    async def config_form(
        enabled: bool = Form(False), qasa_results_url: str = Form(...),
        max_result_pages: int = Form(5), max_result_listings: int = Form(250),
        base_interval_minutes: int = Form(15), jitter_minutes: int = Form(3),
        destinations_json: str = Form("[]"), filters_json: str = Form("{}"),
        sheets_json: str = Form("{}"), discord_json: str = Form("{}"),
        email_json: str = Form("{}"), scb_json: str = Form("{}"), safe_mode: bool = Form(False),
        attribute_furnished: str | None = Form(None),
        attribute_shared: str | None = Form(None),
        attribute_pets_allowed: str | None = Form(None),
        attribute_smoking_allowed: str | None = Form(None),
        attribute_wheelchair_accessible: str | None = Form(None),
        attribute_first_hand: str | None = Form(None),
        attribute_student_home: str | None = Form(None),
        attribute_senior_home: str | None = Form(None),
        attribute_instant_sign: str | None = Form(None),
        attribute_corporate_home: str | None = Form(None),
        filter_minimum_rent: str | None = Form(None),
        filter_maximum_rent: str | None = Form(None),
        filter_minimum_rooms: str | None = Form(None),
        filter_maximum_rooms: str | None = Form(None),
        filter_minimum_area: str | None = Form(None),
        filter_maximum_area: str | None = Form(None),
        filter_allowed_locations: str | None = Form(None),
        filter_excluded_locations: str | None = Form(None),
        filter_required_keywords: str | None = Form(None),
        filter_excluded_keywords: str | None = Form(None),
        filter_availability_from: str | None = Form(None),
        filter_availability_to: str | None = Form(None),
        filter_minimum_population: str | None = Form(None),
        filter_maximum_population: str | None = Form(None),
        filter_maximum_average_age: str | None = Form(None),
        filter_minimum_foreign_background_percent: str | None = Form(None),
        filter_maximum_foreign_background_percent: str | None = Form(None),
        sheets_enabled: bool = Form(False),
        sheets_spreadsheet_id: str | None = Form(None),
        sheets_worksheet: str | None = Form(None),
        discord_enabled: bool = Form(False),
        email_enabled: bool = Form(False),
        email_recipients: str | None = Form(None),
        email_sender: str | None = Form(None),
        email_provider: str | None = Form(None),
        email_smtp_mode: str | None = Form(None),
        email_smtp_host: str | None = Form(None),
        email_smtp_port: str | None = Form(None),
        email_smtp_username: str | None = Form(None),
        email_clear_smtp_username: bool = Form(False),
        email_delivery_mode: str | None = Form(None),
        email_send_no_new: bool = Form(False),
        email_subject: str | None = Form(None),
        scb_data_path: str | None = Form(None),
        scb_id_column: str | None = Form(None),
        scb_name_column: str | None = Form(None),
        scb_vintage: str | None = Form(None),
        scb_crs: str | None = Form(None),
    ):
        try:
            existing = await service.get_config()
            # Secret references are retained server-side because the form never renders them.
            sheets = {
                **existing.sheets.model_dump(),
                **_json_object(sheets_json, "Sheets"),
            }
            sheets["credentials_secret_ref"] = (
                existing.sheets.credentials_secret_ref
            )
            discord = {
                **existing.discord.model_dump(),
                **_json_object(discord_json, "Discord"),
            }
            discord["webhook_secret_ref"] = (
                existing.discord.webhook_secret_ref
            )
            email = {
                **existing.email.model_dump(),
                **_json_object(email_json, "Email"),
            }
            email["smtp_secret_ref"] = existing.email.smtp_secret_ref
            if sheets_spreadsheet_id is not None:
                sheets.update({
                    "enabled": sheets_enabled,
                    "spreadsheet_id": sheets_spreadsheet_id.strip(),
                    "worksheet": (sheets_worksheet or "Listings").strip() or "Listings",
                })
                if sheets_enabled:
                    sheets["credentials_secret_ref"] = (
                        "env:QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON"
                    )
            if discord_enabled or "enabled" in discord:
                discord["enabled"] = discord_enabled
                if discord_enabled:
                    discord["webhook_secret_ref"] = (
                        "env:QASAWATCH_DISCORD_WEBHOOK_URL"
                    )
            if email_sender is not None:
                recipients = [
                    item.strip()
                    for item in email_recipients.replace(";", ",").replace("\n", ",").split(",")
                    if item.strip()
                ] if email_recipients is not None else []
                delivery_mode = (email_delivery_mode or "grouped").strip()
                provider = (email_provider or "custom").strip().lower()
                if provider not in {"gmail", "custom"}:
                    raise ValueError("Choose Gmail or custom email setup")
                sender = email_sender.strip()
                smtp_host = (email_smtp_host or "").strip()
                smtp_port = int((email_smtp_port or "587").strip())
                smtp_mode = (email_smtp_mode or "starttls").strip()
                smtp_username = (
                    (email_smtp_username or "").strip()
                    or (
                        None
                        if email_clear_smtp_username
                        else existing.email.smtp_username
                    )
                )
                if provider == "gmail":
                    smtp_host = "smtp.gmail.com"
                    smtp_port = 587
                    smtp_mode = "starttls"
                    smtp_username = sender or None
                    if not email.get("smtp_secret_ref"):
                        email["smtp_secret_ref"] = "env:QASAWATCH_SMTP_PASSWORD"
                email.update({
                    "enabled": email_enabled,
                    "recipients": recipients,
                    "sender": sender,
                    "smtp_mode": smtp_mode,
                    "smtp_host": smtp_host,
                    "smtp_port": smtp_port,
                    "smtp_username": smtp_username,
                    "grouped": delivery_mode == "grouped",
                    "per_listing": delivery_mode == "per_listing",
                    "send_no_new": email_send_no_new,
                    "subject": (email_subject or "").strip() or "QasaWatch: {count} new listings",
                })
                if email_enabled:
                    email["smtp_secret_ref"] = "env:QASAWATCH_SMTP_PASSWORD"
            raw_destinations = json.loads(destinations_json)
            if not isinstance(raw_destinations, list):
                raise ValueError("Commute destinations must be a list")
            destinations = []
            for index, item in enumerate(raw_destinations, 1):
                if not isinstance(item, dict):
                    raise ValueError(f"Commute destination {index} is invalid")
                label = str(item.get("label") or "").strip()
                address = str(item.get("address") or "").strip()
                maximum = str(item.get("maximum_commute_minutes") or "").strip()
                if not any((label, address, maximum)):
                    continue
                if not address:
                    raise ValueError(
                        f"Commute destination {index} needs an address or station"
                    )
                destinations.append({
                    "label": label or f"Destination {index}",
                    "address": address,
                    "commute_mode": str(
                        item.get("commute_mode") or "arrival"
                    ).strip().lower(),
                    "maximum_commute_minutes": int(maximum) if maximum else None,
                })
            filters = _json_object(filters_json, "Filters")
            filter_controls = {
                "minimum_rent": (filter_minimum_rent, int),
                "maximum_rent": (filter_maximum_rent, int),
                "minimum_rooms": (filter_minimum_rooms, float),
                "maximum_rooms": (filter_maximum_rooms, float),
                "minimum_area": (filter_minimum_area, float),
                "maximum_area": (filter_maximum_area, float),
                "minimum_population": (filter_minimum_population, int),
                "maximum_population": (filter_maximum_population, int),
                "maximum_average_age": (filter_maximum_average_age, float),
                "minimum_foreign_background_percent": (
                    filter_minimum_foreign_background_percent,
                    float,
                ),
                "maximum_foreign_background_percent": (
                    filter_maximum_foreign_background_percent,
                    float,
                ),
            }
            if any(value is not None for value, _ in filter_controls.values()):
                for key, (raw_value, converter) in filter_controls.items():
                    value = (raw_value or "").strip()
                    filters[key] = converter(value) if value else None
                for key, raw_value in {
                    "allowed_locations": filter_allowed_locations,
                    "excluded_locations": filter_excluded_locations,
                    "required_keywords": filter_required_keywords,
                    "excluded_keywords": filter_excluded_keywords,
                }.items():
                    filters[key] = [
                        item.strip()
                        for item in (raw_value or "").replace(";", ",").replace("\n", ",").split(",")
                        if item.strip()
                    ]
                filters["availability_from"] = (filter_availability_from or "").strip() or None
                filters["availability_to"] = (filter_availability_to or "").strip() or None
            attribute_requirements = dict(filters.get("attribute_requirements") or {})
            attribute_controls = {
                "furnished": attribute_furnished,
                "shared": attribute_shared,
                "pets_allowed": attribute_pets_allowed,
                "smoking_allowed": attribute_smoking_allowed,
                "wheelchair_accessible": attribute_wheelchair_accessible,
                "first_hand": attribute_first_hand,
                "student_home": attribute_student_home,
                "senior_home": attribute_senior_home,
                "instant_sign": attribute_instant_sign,
                "corporate_home": attribute_corporate_home,
            }
            for key, raw_value in attribute_controls.items():
                if raw_value is None:
                    continue
                value = raw_value.strip().lower()
                if value == "ignore":
                    attribute_requirements.pop(key, None)
                elif value in {"true", "false"}:
                    attribute_requirements[key] = value == "true"
                else:
                    raise ValueError(f"invalid attribute requirement for {key}")
            filters["attribute_requirements"] = attribute_requirements
            scb = {
                **existing.scb.model_dump(),
                **_json_object(scb_json, "SCB"),
            }
            if scb_data_path is not None:
                scb.update({
                    "data_path": scb_data_path.strip(),
                    "id_column": (scb_id_column or "").strip() or "municipality_id",
                    "name_column": (scb_name_column or "").strip() or "municipality_name",
                    "vintage": (scb_vintage or "").strip(),
                    "crs": (scb_crs or "").strip() or "EPSG:4326",
                })
            config = WatcherConfig(
                enabled=enabled, qasa_results_url=qasa_results_url,
                max_result_pages=max_result_pages,
                max_result_listings=max_result_listings,
                base_interval_minutes=base_interval_minutes, jitter_minutes=jitter_minutes,
                destinations=destinations, filters=filters,
                sheets=sheets, discord=discord, email=email, scb=scb, safe_mode=safe_mode,
                maps_api_secret_ref=(
                    "env:QASAWATCH_GOOGLE_MAPS_API_KEY"
                    if destinations or scb.get("data_path")
                    else existing.maps_api_secret_ref
                ),
            )
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                message = "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                    for error in exc.errors()
                )
            elif isinstance(exc, json.JSONDecodeError):
                message = "One of the advanced JSON fields is invalid"
            else:
                message = str(exc)
            return RedirectResponse(
                f"/?config_error={quote('Configuration was not saved: ' + message)}",
                status_code=303,
            )
        await service.save_config(config)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/run-now")
    async def run_now():
        try:
            return await service.scheduler.run_now()
        except BrowserHostError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/manual")
    async def manual(payload: ManualRequest, request: Request):
        try:
            history_id, result = await service.process_manual(payload.url, requested_by=request.client.host if request.client else None)
        except IncompletePageError as exc:
            raise HTTPException(422, str(exc)) from exc
        except BrowserHostError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"manual_id": history_id, **_result_json(result)}

    @app.post("/manual", response_class=HTMLResponse)
    async def manual_form(request: Request, url: str = Form(...)):
        try:
            payload = ManualRequest(url=url)
            history_id, result = await service.process_manual(payload.url, requested_by=request.client.host if request.client else None)
            state = await service.dashboard()
            state.update({"manual_result": _result_json(result), "manual_id": history_id})
            return TEMPLATES.TemplateResponse(request, "dashboard.html", state)
        except (
            ValidationError,
            ValueError,
            IncompletePageError,
            BrowserHostError,
        ) as exc:
            state = await service.dashboard()
            state["manual_error"] = str(exc)
            status_code = 503 if isinstance(exc, BrowserHostError) else 422
            return TEMPLATES.TemplateResponse(
                request, "dashboard.html", state, status_code=status_code
            )

    @app.post("/api/manual/promote")
    async def promote(payload: PromotionRequest):
        try:
            return _result_json(await service.promote_manual(payload))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/manual/promote", response_class=HTMLResponse)
    async def promote_form(
        request: Request,
        manual_id: int = Form(...),
        sheets: bool = Form(False),
        discord: bool = Form(False),
        email: bool = Form(False),
    ):
        channels = [
            name for name, enabled in (
                ("sheets", sheets), ("discord", discord), ("email", email)
            ) if enabled
        ]
        state = await service.dashboard()
        try:
            result = await service.promote_manual(
                PromotionRequest(manual_id=manual_id, channels=channels)
            )
            state.update({
                "manual_result": _result_json(result),
                "manual_id": manual_id,
                "manual_promoted": True,
            })
            return TEMPLATES.TemplateResponse(request, "dashboard.html", state)
        except (LookupError, PermissionError) as exc:
            state["manual_error"] = str(exc)
            return TEMPLATES.TemplateResponse(
                request, "dashboard.html", state, status_code=409
            )

    @app.post("/api/retry")
    async def retry(payload: RetryRequest):
        try:
            return _result_json(await service.retry_listing(payload.listing_id, payload.channels))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/test-email")
    async def test_email(payload: TestEmailRequest):
        return await service.test_email(payload.recipient)

    @app.post("/test-email")
    async def test_email_form(recipient: str | None = Form(None)):
        await service.test_email(recipient or None)
        return RedirectResponse("/", status_code=303)

    @app.post("/test-discord")
    async def test_discord_form():
        await service.test_discord()
        return RedirectResponse("/#discord-settings", status_code=303)

    @app.post("/test-maps")
    async def test_maps_form():
        await service.test_maps()
        return RedirectResponse("/#connections-heading", status_code=303)

    @app.post("/test-sheets")
    async def test_sheets_form():
        await service.test_sheets()
        return RedirectResponse("/#sheets-settings", status_code=303)

    @app.post("/api/email-batches/{batch_id}/retry")
    async def retry_email_batch(batch_id: int):
        try:
            return await service.retry_email_batch(batch_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/email-batches/{batch_id}/resolve")
    async def resolve_email_batch(batch_id: int, delivered: bool):
        try:
            await service.resolve_email_review(batch_id, delivered=delivered)
            return {"batch_id": batch_id, "resolved": True, "delivered": delivered}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/listings/{listing_id}/deliveries/{channel}/resolve")
    async def resolve_delivery(listing_id: int, channel: str, delivered: bool):
        try:
            await service.resolve_delivery_review(
                listing_id, channel, delivered=delivered
            )
            return {
                "listing_id": listing_id,
                "channel": channel,
                "resolved": True,
                "delivered": delivered,
            }
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    app.state.service = service
    return app


def _result_json(result) -> dict:
    return {
        "listing_id": result.listing_id, "stage": result.stage.value,
        "duplicate": result.duplicate, "data": dict(result.data),
        "delivery_failures": list(result.delivery_failures),
        "delivery_statuses": {
            channel: dict(status)
            for channel, status in result.delivery_statuses.items()
        },
        "accepted": result.decision.accepted if result.decision else None,
        "rejection_reasons": [
            {"code": reason.code, "message": reason.message, "source": reason.source.value, "rule": reason.rule, "details": dict(reason.details)}
            for reason in (result.decision.reasons if result.decision else ())
        ],
    }


def _run_json(run) -> dict:
    return {"id": run.id, "status": run.status, "stats": run.stats, "started_at": run.started_at, "finished_at": run.finished_at, "error": run.error}
