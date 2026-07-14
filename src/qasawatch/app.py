"""FastAPI operator dashboard and JSON API."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .scheduler import WatchScheduler
from .schemas import ManualRequest, PromotionRequest, RetryRequest, TestEmailRequest, WatcherConfig
from .service import AppService, IncompletePageError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


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
                # Do not expose provider/browser exception bodies in HTML.
                service.last_browser_state = {
                    "status": "error",
                    "errors": [f"browser startup failed ({type(exc).__name__})"],
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
        return TEMPLATES.TemplateResponse(request, "dashboard.html", await service.dashboard())

    @app.get("/api/status")
    async def api_status():
        state = await service.dashboard()
        return {
            "watcher": state["watcher"], "browser": state["browser"],
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
        base_interval_minutes: int = Form(15), jitter_minutes: int = Form(3),
        destinations_json: str = Form("[]"), filters_json: str = Form("{}"),
        sheets_json: str = Form("{}"), discord_json: str = Form("{}"),
        email_json: str = Form("{}"), scb_json: str = Form("{}"), safe_mode: bool = Form(False),
        maps_api_secret_ref: str = Form(""),
        sheets_credentials_secret_ref: str = Form(""),
        discord_webhook_secret_ref: str = Form(""),
        smtp_secret_ref: str = Form(""),
    ):
        try:
            existing = await service.get_config()
            # Secret references are retained server-side because the form never renders them.
            sheets = {**existing.sheets.model_dump(), **json.loads(sheets_json)}
            discord = {**existing.discord.model_dump(), **json.loads(discord_json)}
            email = {**existing.email.model_dump(), **json.loads(email_json)}
            if sheets_credentials_secret_ref.strip():
                sheets["credentials_secret_ref"] = sheets_credentials_secret_ref.strip()
            if discord_webhook_secret_ref.strip():
                discord["webhook_secret_ref"] = discord_webhook_secret_ref.strip()
            if smtp_secret_ref.strip():
                email["smtp_secret_ref"] = smtp_secret_ref.strip()
            config = WatcherConfig(
                enabled=enabled, qasa_results_url=qasa_results_url,
                base_interval_minutes=base_interval_minutes, jitter_minutes=jitter_minutes,
                destinations=json.loads(destinations_json), filters=json.loads(filters_json),
                sheets=sheets, discord=discord, email=email, scb=json.loads(scb_json), safe_mode=safe_mode,
                maps_api_secret_ref=(
                    maps_api_secret_ref.strip()
                    or existing.maps_api_secret_ref
                ),
            )
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(422, str(exc)) from exc
        await service.save_config(config)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/run-now")
    async def run_now():
        return await service.scheduler.run_now()

    @app.post("/api/manual")
    async def manual(payload: ManualRequest, request: Request):
        try:
            history_id, result = await service.process_manual(payload.url, requested_by=request.client.host if request.client else None)
        except IncompletePageError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"manual_id": history_id, **_result_json(result)}

    @app.post("/manual", response_class=HTMLResponse)
    async def manual_form(request: Request, url: str = Form(...)):
        try:
            payload = ManualRequest(url=url)
            history_id, result = await service.process_manual(payload.url, requested_by=request.client.host if request.client else None)
            state = await service.dashboard()
            state.update({"manual_result": _result_json(result), "manual_id": history_id})
            return TEMPLATES.TemplateResponse(request, "dashboard.html", state)
        except (ValidationError, ValueError, IncompletePageError) as exc:
            state = await service.dashboard()
            state["manual_error"] = str(exc)
            return TEMPLATES.TemplateResponse(request, "dashboard.html", state, status_code=422)

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
        "accepted": result.decision.accepted if result.decision else None,
        "rejection_reasons": [
            {"code": reason.code, "message": reason.message, "source": reason.source.value, "rule": reason.rule, "details": dict(reason.details)}
            for reason in (result.decision.reasons if result.decision else ())
        ],
    }


def _run_json(run) -> dict:
    return {"id": run.id, "status": run.status, "stats": run.stats, "started_at": run.started_at, "finished_at": run.finished_at, "error": run.error}
