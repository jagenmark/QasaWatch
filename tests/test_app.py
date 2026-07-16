from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from qasawatch.app import create_app
from qasawatch.db import Database
from qasawatch.models import ProcessingError, Run
from qasawatch.pipeline import Pipeline
from qasawatch.schemas import FilterSettings, WatcherConfig
from qasawatch.service import AppService


class NoBrowser:
    async def scan(self, url): raise AssertionError("browser should not be called")


class StartupBrowser(NoBrowser):
    def __init__(self): self.connected = False
    async def connect(self): self.connected = True
    async def close(self): pass


async def test_config_api_redacts_all_secret_references(tmp_path):
    db = Database(tmp_path / "state.db"); await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    config = WatcherConfig.model_validate({
        "sheets": {"credentials_secret_ref": "env:PRIVATE_SHEETS_42"},
        "discord": {"webhook_secret_ref": "env:PRIVATE_DISCORD_42"},
        "email": {"smtp_secret_ref": "env:PRIVATE_SMTP_42"},
    })
    await service.save_config(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test") as client:
        response = await client.get("/api/config")
        dashboard = await client.get("/")
        invalid = await client.post("/api/manual", json={"url": "https://evil.test/home/1"})
    text = response.text
    assert response.status_code == 200 and "env:" not in text and "PRIVATE_SHEETS_42" not in text
    assert response.json()["discord"]["secret_configured"] is True
    assert all(secret not in dashboard.text for secret in ("PRIVATE_SHEETS_42", "PRIVATE_DISCORD_42", "PRIVATE_SMTP_42"))
    assert invalid.status_code == 422
    await db.dispose()


async def test_disabled_watcher_starts_browser_for_manual_profile_access(tmp_path):
    db = Database(tmp_path / "browser-start.db")
    browser = StartupBrowser()
    service = AppService(db, browser, Pipeline(db))
    app = create_app(service, start_scheduler=False)

    async with app.router.lifespan_context(app):
        assert browser.connected
        assert service.last_browser_state["status"] == "running"


def test_filter_attribute_requirements_reject_unknown_keys():
    with pytest.raises(ValidationError):
        FilterSettings(attribute_requirements={"unbounded_attribute": True})


def test_enabled_watcher_accepts_one_destination_but_not_zero():
    config = WatcherConfig(
        enabled=True,
        destinations=[
            {
                "label": "T-Centralen",
                "address": "T-Centralen, Stockholm",
                "commute_mode": "arrival",
            }
        ],
    )
    assert len(config.destinations) == 1
    with pytest.raises(ValidationError, match="at least one destination"):
        WatcherConfig(enabled=True)


async def test_config_form_persists_false_and_removes_ignored_attribute(tmp_path):
    db = Database(tmp_path / "attribute-form.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    await service.save_config(WatcherConfig(filters={
        "minimum_rent": 5000,
        "attribute_requirements": {"furnished": True, "pets_allowed": False},
    }))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        dashboard = await client.get("/")
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "filters_json": '{"minimum_rent": 5000, "attribute_requirements": {"furnished": true, "pets_allowed": false}}',
            "attribute_furnished": "false",
            "attribute_pets_allowed": "ignore",
        })

    assert 'name="attribute_furnished"' in dashboard.text
    assert 'name="attribute_shared"' in dashboard.text
    assert 'name="attribute_pets_allowed"' in dashboard.text
    furnished_control = dashboard.text.split('name="attribute_furnished"', 1)[1].split("</select>", 1)[0]
    pets_control = dashboard.text.split('name="attribute_pets_allowed"', 1)[1].split("</select>", 1)[0]
    shared_control = dashboard.text.split('name="attribute_shared"', 1)[1].split("</select>", 1)[0]
    assert 'value="true" selected' in furnished_control
    assert 'value="false" selected' in pets_control
    assert 'value="ignore" selected' in shared_control
    assert response.status_code == 303
    saved = await service.get_config()
    assert saved.filters.minimum_rent == 5000
    assert saved.filters.attribute_requirements == {"furnished": False}
    await db.dispose()


async def test_config_form_saves_two_plain_destination_controls(tmp_path):
    db = Database(tmp_path / "destination-form.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post("/config", data={
            "enabled": "true",
            "safe_mode": "true",
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "destination_1_label": "Enköping",
            "destination_1_address": "Enköping centralstation",
            "destination_1_mode": "arrival",
            "destination_1_maximum": "75",
            "destination_2_label": "T-Centralen",
            "destination_2_address": "T-Centralen, Stockholm",
            "destination_2_mode": "departure",
            "destination_2_maximum": "45",
        })

    assert response.status_code == 303
    saved = await service.get_config()
    assert saved.enabled
    assert [item.address for item in saved.destinations] == [
        "Enköping centralstation",
        "T-Centralen, Stockholm",
    ]
    assert saved.destinations[0].commute_mode == "arrival"
    assert saved.destinations[1].commute_mode == "departure"
    assert saved.destinations[0].maximum_commute_minutes == 75
    await db.dispose()


async def test_invalid_advanced_json_returns_readable_dashboard_error(tmp_path):
    db = Database(tmp_path / "config-error.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "filters_json": "[not JSON",
        })

    assert response.status_code == 200
    assert "Configuration was not saved" in response.text
    assert "advanced JSON" in response.text
    assert (await service.get_config()).qasa_results_url == "https://qasa.com/se/sv/find-home"
    await db.dispose()


async def test_normal_provider_and_filter_controls_save_without_editing_json(tmp_path):
    db = Database(tmp_path / "friendly-form.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        dashboard = await client.get("/")
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "filter_maximum_rent": "10300",
            "filter_minimum_area": "25",
            "filter_allowed_locations": "Solna\nSundbyberg",
            "sheets_enabled": "true",
            "sheets_spreadsheet_id": "sheet-123",
            "sheets_worksheet": "Apartments",
            "discord_enabled": "true",
            "email_enabled": "true",
            "email_recipients": "one@example.com, two@example.com",
            "email_sender": "watcher@example.com",
            "email_smtp_host": "smtp.example.com",
            "email_smtp_port": "587",
            "email_smtp_mode": "starttls",
            "email_delivery_mode": "per_listing",
            "email_subject": "Qasa: {count}",
            "scb_data_path": "data/scb/sweden.geojson",
            "scb_id_column": "deso_id",
            "scb_name_column": "deso_name",
            "scb_vintage": "2025",
            "scb_crs": "EPSG:4326",
        })

    assert response.status_code == 303
    assert "<summary>Advanced JSON controls</summary>" in dashboard.text
    assert 'name="email_recipients"' in dashboard.text
    assert 'name="sheets_spreadsheet_id"' in dashboard.text
    saved = await service.get_config()
    assert saved.filters.maximum_rent == 10300
    assert saved.filters.minimum_area == 25
    assert saved.filters.allowed_locations == ["Solna", "Sundbyberg"]
    assert saved.sheets.enabled and saved.sheets.spreadsheet_id == "sheet-123"
    assert saved.discord.enabled
    assert saved.email.enabled
    assert saved.email.recipients == ["one@example.com", "two@example.com"]
    assert saved.email.per_listing and not saved.email.grouped
    assert saved.scb.data_path == "data/scb/sweden.geojson"
    assert saved.scb.id_column == "deso_id"
    await db.dispose()


async def test_dashboard_test_email_action_shows_result_without_exposing_secret(tmp_path):
    db = Database(tmp_path / "test-email-form.db")
    await db.initialize()
    calls = []

    async def email_tester(recipient):
        calls.append(recipient)
        return {"test": True}

    service = AppService(
        db,
        NoBrowser(),
        Pipeline(db),
        email_tester=email_tester,
    )
    await service.save_config(WatcherConfig(
        safe_mode=False,
        email={
            "enabled": True,
            "recipients": ["saved@example.com"],
            "sender": "watcher@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_secret_ref": "env:QASAWATCH_SMTP_PASSWORD",
        },
    ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        response = await client.post(
            "/test-email",
            data={"recipient": "override@example.com"},
        )

    assert response.status_code == 200
    assert calls == ["override@example.com"]
    assert "succeeded" in response.text
    assert 'value="env:QASAWATCH_SMTP_PASSWORD"' not in response.text
    await db.dispose()


async def test_dashboard_can_clear_redacted_smtp_username(tmp_path):
    db = Database(tmp_path / "clear-smtp-user.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    await service.save_config(WatcherConfig(email={
        "smtp_username": "old@example.com",
    }))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "email_sender": "new@example.com",
            "email_smtp_host": "smtp.example.com",
            "email_smtp_port": "587",
            "email_smtp_mode": "starttls",
            "email_delivery_mode": "grouped",
            "email_clear_smtp_username": "true",
        })

    assert response.status_code == 303
    assert (await service.get_config()).email.smtp_username is None
    await db.dispose()


async def test_dashboard_error_history_includes_time_and_run_link(tmp_path):
    db = Database(tmp_path / "error-history.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    occurred = datetime(2026, 7, 16, 9, 30, 15, tzinfo=UTC)
    async with db.sessions.begin() as session:
        run = Run(status="failed")
        session.add(run)
        await session.flush()
        run_id = run.id
        session.add(ProcessingError(
            run_id=run_id,
            operation="safe_processing:123",
            error_type="RuntimeError",
            message="route API unavailable",
            created_at=occurred,
        ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Recent error history" in response.text
    assert "2026-07-16 09:30:15" in response.text
    assert f'href="#run-{run_id}"' in response.text
    assert "route API unavailable" in response.text
    await db.dispose()
