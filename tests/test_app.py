from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from qasawatch.app import create_app
from qasawatch.browser_host import BrowserHostError
from qasawatch.db import Database
from qasawatch.models import Listing, ProcessingError, Run
from qasawatch.pipeline import Pipeline
from qasawatch.schemas import FilterSettings, WatcherConfig
from qasawatch.service import AppService


class NoBrowser:
    async def scan(self, url): raise AssertionError("browser should not be called")


class StartupBrowser(NoBrowser):
    def __init__(self): self.connected = False
    async def connect(self): self.connected = True
    async def close(self): pass


class FailingBrowser:
    async def scan(self, url, *, results_only=False):
        raise BrowserHostError("Chrome needs a graphical display")


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


async def test_manual_browser_failure_returns_actionable_service_unavailable(tmp_path):
    db = Database(tmp_path / "browser-failure.db")
    await db.initialize()
    service = AppService(db, FailingBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://test",
    ) as client:
        api_response = await client.post(
            "/api/manual", json={"url": "https://qasa.com/se/sv/home/example"}
        )
        form_response = await client.post(
            "/manual", data={"url": "https://qasa.com/se/sv/home/example"}
        )

    assert api_response.status_code == 503
    assert api_response.json()["detail"] == "Chrome needs a graphical display"
    assert form_response.status_code == 503
    assert "Chrome needs a graphical display" in form_response.text
    await db.dispose()


async def test_discord_dashboard_uses_connection_status_and_test_action(tmp_path, monkeypatch):
    db = Database(tmp_path / "discord-dashboard.db")
    await db.initialize()
    calls = []

    async def tester():
        calls.append(True)
        return {"message_id": "test-1"}

    monkeypatch.setenv(
        "QASAWATCH_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/example/token",
    )
    service = AppService(
        db,
        NoBrowser(),
        Pipeline(db),
        discord_tester=tester,
    )
    await service.save_config(WatcherConfig(discord={
        "enabled": True,
        "webhook_secret_ref": "env:QASAWATCH_DISCORD_WEBHOOK_URL",
    }))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        dashboard = await client.get("/")
        tested = await client.post("/test-discord")

    assert "Webhook available in the running environment" in dashboard.text
    assert "Webhook reference" not in dashboard.text
    assert "https://discord.com/api/webhooks/example/token" not in dashboard.text
    assert calls == [True]
    assert "Completed" in tested.text
    await db.dispose()


async def test_enabling_discord_automatically_uses_standard_env_variable(tmp_path):
    db = Database(tmp_path / "discord-default-reference.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "discord_enabled": "true",
        })

    assert response.status_code == 303
    saved = await service.get_config()
    assert saved.discord.enabled
    assert (
        saved.discord.webhook_secret_ref
        == "env:QASAWATCH_DISCORD_WEBHOOK_URL"
    )
    await db.dispose()


async def test_failed_discord_test_updates_the_discord_card(tmp_path, monkeypatch):
    db = Database(tmp_path / "failed-discord-card.db")
    await db.initialize()

    async def tester():
        raise RuntimeError("webhook rejected token=private-discord-token")

    monkeypatch.setenv(
        "QASAWATCH_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/example/private-discord-token",
    )
    service = AppService(
        db,
        NoBrowser(),
        Pipeline(db),
        discord_tester=tester,
    )
    await service.save_config(WatcherConfig(discord={
        "enabled": True,
        "webhook_secret_ref": "env:QASAWATCH_DISCORD_WEBHOOK_URL",
    }))

    await service.test_discord()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get("/")

    discord_card = response.text.split('id="discord-settings"', 1)[1].split(
        "</div>", 1
    )[0]
    assert "Needs attention" in discord_card
    assert "private-discord-token" not in response.text
    await db.dispose()


async def test_maps_and_sheets_dashboard_tests_use_resolved_credentials_without_writes(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "maps-sheets-dashboard.db")
    await db.initialize()
    calls = []

    async def maps_tester():
        calls.append("maps")
        return {"geocoding": "ok", "routes": "ok"}

    async def sheets_tester():
        calls.append("sheets")
        return {"spreadsheet": "Apartment search", "worksheet": "Listings"}

    monkeypatch.setenv("QASAWATCH_GOOGLE_MAPS_API_KEY", "test-maps-key")
    monkeypatch.setenv(
        "QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON", "test-service-account"
    )
    service = AppService(
        db,
        NoBrowser(),
        Pipeline(db),
        maps_tester=maps_tester,
        sheets_tester=sheets_tester,
    )
    await service.save_config(WatcherConfig(
        destinations=[{
            "label": "Work",
            "address": "T-Centralen, Stockholm",
        }],
        maps_api_secret_ref="env:QASAWATCH_GOOGLE_MAPS_API_KEY",
        sheets={
            "enabled": True,
            "spreadsheet_id": "sheet-123",
            "worksheet": "Listings",
            "credentials_secret_ref": (
                "env:QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON"
            ),
        },
    ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        dashboard = await client.get("/")
        maps_result = await client.post("/test-maps")
        sheets_result = await client.post("/test-sheets")
        status = await client.get("/api/status")

    assert 'form="test-maps-form"' in dashboard.text
    assert 'form="test-sheets-form"' in dashboard.text
    assert calls == ["maps", "sheets"]
    assert "Google Maps: Completed" in maps_result.text
    assert "Google Sheets: Completed" in sheets_result.text
    assert status.json()["connections"]["maps"]["connected"] is True
    assert status.json()["connections"]["sheets"]["connected"] is True
    assert "test-maps-key" not in maps_result.text
    assert "test-service-account" not in sheets_result.text
    await db.dispose()


async def test_failed_connection_test_downgrades_main_status_badge(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "failed-maps-status.db")
    await db.initialize()

    async def maps_tester():
        raise RuntimeError("Google Maps rejected api_key=private-value")

    monkeypatch.setenv("QASAWATCH_GOOGLE_MAPS_API_KEY", "private-value")
    service = AppService(
        db,
        NoBrowser(),
        Pipeline(db),
        maps_tester=maps_tester,
    )
    await service.save_config(WatcherConfig(
        destinations=[{"label": "Work", "address": "Stockholm"}],
        maps_api_secret_ref="env:QASAWATCH_GOOGLE_MAPS_API_KEY",
    ))

    await service.test_maps()
    state = await service.dashboard()

    assert state["connections"]["maps"]["status_label"] == "Needs attention"
    assert state["connections"]["maps"]["status_tone"] == "danger"
    assert "private-value" not in str(state["connections"]["maps"])
    await db.dispose()


async def test_existing_enabled_connections_adopt_standard_references(tmp_path):
    db = Database(tmp_path / "standard-ref-migration.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    await service.config_store.set_value("watcher.config", {
        "destinations": [{"label": "Work", "address": "Stockholm"}],
        "sheets": {"enabled": True, "spreadsheet_id": "sheet"},
        "discord": {"enabled": True},
        "email": {
            "enabled": True,
            "recipients": ["person@example.com"],
            "sender": "sender@example.com",
            "smtp_host": "smtp.example.com",
        },
    })

    config = await service.get_config()

    assert config.maps_api_secret_ref == "env:QASAWATCH_GOOGLE_MAPS_API_KEY"
    assert (
        config.sheets.credentials_secret_ref
        == "env:QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    assert (
        config.discord.webhook_secret_ref
        == "env:QASAWATCH_DISCORD_WEBHOOK_URL"
    )
    assert config.email.smtp_secret_ref == "env:QASAWATCH_SMTP_PASSWORD"
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


def test_enabled_watcher_allows_any_number_of_optional_destinations():
    without_commute = WatcherConfig(enabled=True)
    assert without_commute.destinations == []
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


async def test_config_form_saves_any_number_of_commute_destinations(tmp_path):
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
            "destinations_json": """[
                {"label":"Enköping","address":"Enköping centralstation","commute_mode":"arrival","maximum_commute_minutes":75},
                {"label":"T-Centralen","address":"T-Centralen, Stockholm","commute_mode":"departure","maximum_commute_minutes":45},
                {"label":"Campus","address":"Albano, Stockholm","commute_mode":"arrival","maximum_commute_minutes":30}
            ]""",
        })

    assert response.status_code == 303
    saved = await service.get_config()
    assert saved.enabled
    assert [item.address for item in saved.destinations] == [
        "Enköping centralstation",
        "T-Centralen, Stockholm",
        "Albano, Stockholm",
    ]
    assert saved.destinations[0].commute_mode == "arrival"
    assert saved.destinations[1].commute_mode == "departure"
    assert saved.destinations[0].maximum_commute_minutes == 75
    await db.dispose()


async def test_enabled_watcher_saves_without_commute_or_google_maps(tmp_path):
    db = Database(tmp_path / "no-commute-form.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        dashboard = await client.get("/")
        response = await client.post("/config", data={
            "enabled": "true",
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "destinations_json": "[]",
        })

    assert response.status_code == 303
    saved = await service.get_config()
    assert saved.enabled
    assert saved.destinations == []
    assert saved.maps_api_secret_ref is None
    assert "Optional commute destinations" in dashboard.text
    assert "Add another commute destination" in dashboard.text
    assert "Google Maps may still be used to locate listings for SCB demographics" in dashboard.text
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


async def test_dashboard_json_cannot_replace_secret_environment_references(tmp_path):
    db = Database(tmp_path / "dashboard-secret-ref.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "sheets_json": (
                '{"enabled":true,"spreadsheet_id":"sheet",'
                '"credentials_secret_ref":"env:AWS_SECRET_ACCESS_KEY"}'
            ),
            "discord_json": (
                '{"enabled":true,'
                '"webhook_secret_ref":"env:AWS_SECRET_ACCESS_KEY"}'
            ),
            "email_json": (
                '{"enabled":true,"smtp_secret_ref":"env:AWS_SECRET_ACCESS_KEY"}'
            ),
            "sheets_enabled": "true",
            "sheets_spreadsheet_id": "sheet",
            "discord_enabled": "true",
            "email_enabled": "true",
            "email_sender": "sender@example.com",
            "email_provider": "custom",
            "email_recipients": "person@example.com",
            "email_smtp_host": "smtp.example.com",
        })

    assert response.status_code == 303
    config = await service.get_config()
    assert "AWS_SECRET_ACCESS_KEY" not in config.model_dump_json()
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
            "filter_minimum_foreign_background_percent": "20",
            "filter_maximum_foreign_background_percent": "50",
            "filter_allowed_locations": "Solna\nSundbyberg",
            "sheets_enabled": "true",
            "sheets_spreadsheet_id": "sheet-123",
            "sheets_worksheet": "Apartments",
            "discord_enabled": "true",
            "email_enabled": "true",
            "email_recipients": "one@example.com, two@example.com",
            "email_sender": "watcher@example.com",
            "email_provider": "custom",
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
    assert "Credential reference" not in dashboard.text
    assert "Google Maps key reference" not in dashboard.text
    assert "Password location" not in dashboard.text
    saved = await service.get_config()
    assert saved.filters.maximum_rent == 10300
    assert saved.filters.minimum_area == 25
    assert saved.filters.minimum_foreign_background_percent == 20
    assert saved.filters.maximum_foreign_background_percent == 50
    assert saved.filters.allowed_locations == ["Solna", "Sundbyberg"]
    assert saved.sheets.enabled and saved.sheets.spreadsheet_id == "sheet-123"
    assert (
        saved.sheets.credentials_secret_ref
        == "env:QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    assert saved.discord.enabled
    assert (
        saved.discord.webhook_secret_ref
        == "env:QASAWATCH_DISCORD_WEBHOOK_URL"
    )
    assert saved.email.enabled
    assert saved.email.smtp_secret_ref == "env:QASAWATCH_SMTP_PASSWORD"
    assert saved.email.recipients == ["one@example.com", "two@example.com"]
    assert saved.email.per_listing and not saved.email.grouped
    assert saved.scb.data_path == "data/scb/sweden.geojson"
    assert saved.scb.id_column == "deso_id"
    assert saved.maps_api_secret_ref == "env:QASAWATCH_GOOGLE_MAPS_API_KEY"
    assert "Longest commute (minutes)" not in dashboard.text
    assert "Minimum foreign background nearby (%)" in dashboard.text
    assert "Maximum foreign background nearby (%)" in dashboard.text
    await db.dispose()


async def test_gmail_setup_chooses_safe_defaults_without_server_fields(tmp_path):
    db = Database(tmp_path / "gmail-form.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        dashboard = await client.get("/")
        response = await client.post("/config", data={
            "qasa_results_url": "https://qasa.com/se/sv/find-home",
            "email_enabled": "true",
            "email_provider": "gmail",
            "email_recipients": "recipient@example.com",
            "email_sender": "sender@gmail.com",
            "email_delivery_mode": "grouped",
        })

    assert response.status_code == 303
    assert "Gmail — easiest setup" in dashboard.text
    assert "Open Google App Passwords" in dashboard.text
    saved = await service.get_config()
    assert saved.email.smtp_host == "smtp.gmail.com"
    assert saved.email.smtp_port == 587
    assert saved.email.smtp_mode == "starttls"
    assert saved.email.smtp_username == "sender@gmail.com"
    assert saved.email.smtp_secret_ref == "env:QASAWATCH_SMTP_PASSWORD"
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
    assert "Completed" in response.text
    assert 'value="env:QASAWATCH_SMTP_PASSWORD"' not in response.text
    await db.dispose()


async def test_dashboard_test_email_failure_shows_redacted_reason(tmp_path):
    db = Database(tmp_path / "failed-test-email-form.db")
    await db.initialize()

    async def email_tester(_recipient):
        raise RuntimeError(
            "SMTP rejected Basic abc123 and https://user:mail-password@smtp.example.com/send"
        )

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
        response = await client.post("/test-email", data={})

    assert response.status_code == 200
    assert "Needs attention" in response.text
    assert "SMTP rejected Basic &lt;redacted&gt;" in response.text
    assert "mail-password" not in response.text
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


async def test_dashboard_troubleshooting_uses_stockholm_time_and_run_link(tmp_path):
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
    assert "Problems and troubleshooting" in response.text
    assert "16 July 2026, 11:30" in response.text
    assert "16 July 2026, 09:30" not in response.text
    assert f'href="#run-{run_id}"' in response.text
    assert "route API unavailable" in response.text
    assert '<details class="details-box activity-details">' in response.text
    await db.dispose()


async def test_dashboard_redacts_stored_processing_error_messages(tmp_path):
    db = Database(tmp_path / "redacted-processing-error.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    async with db.sessions.begin() as session:
        session.add(ProcessingError(
            operation="provider-call",
            error_type="RuntimeError",
            message="request failed with Bearer stored-secret-token",
        ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert "request failed with Bearer &lt;redacted&gt;" in response.text
    assert "stored-secret-token" not in response.text
    await db.dispose()


async def test_dashboard_failed_run_keeps_error_in_collapsed_details(tmp_path):
    db = Database(tmp_path / "failed-run-details.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    async with db.sessions.begin() as session:
        session.add(Run(
            status="failed",
            error="Browser check failed with Bearer top-secret-token",
            started_at=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
        ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "<summary>What went wrong</summary>" in response.text
    assert "Browser check failed with Bearer &lt;redacted&gt;" in response.text
    assert "top-secret-token" not in response.text
    assert "No recent problems." in response.text
    await db.dispose()


async def test_dashboard_formats_schedule_and_hides_coordination_details(tmp_path):
    db = Database(tmp_path / "schedule-display.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    app = create_app(service)
    await service.config_store.set_value(
        "scheduler.next_run",
        "2026-07-16T11:44:07.808025+02:00",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "16 July 2026, 11:44" in response.text
    assert "2026-07-16T11:44:07.808025+02:00" not in response.text
    assert "Scan lock" not in response.text
    assert "<summary>System details</summary>" in response.text
    assert "Check coordination" in response.text
    assert '<h2 id="connections-heading">Service status</h2>' in response.text
    assert response.text.index('id="connections-heading"') < response.text.index('id="one-listing-heading"')
    assert "<summary>Connection status</summary>" not in response.text
    assert "All times are shown in Europe/Stockholm." in response.text
    assert 'id="run-now-form"' in response.text
    assert 'id="run-result-dialog"' in response.text
    assert 'id="run-result-total-available"' in response.text
    assert 'id="run-result-pages-scanned"' in response.text
    assert 'src="/static/dashboard.js?v=20260716-8"' in response.text
    assert 'id="live-next-check"' in response.text
    assert 'id="live-system-details"' in response.text
    assert 'id="live-activity"' in response.text
    assert "data-activity-version=" in response.text
    assert "<summary>Latest checks" in response.text
    assert "<summary>Latest homes" in response.text
    await db.dispose()


async def test_dashboard_stockholm_time_handles_winter_cet(tmp_path):
    db = Database(tmp_path / "winter-time.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    async with db.sessions.begin() as session:
        session.add(ProcessingError(
            operation="winter-check",
            error_type="RuntimeError",
            message="winter diagnostic",
            created_at=datetime(2026, 1, 16, 9, 30, tzinfo=UTC),
        ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert "16 January 2026, 10:30" in response.text
    await db.dispose()


async def test_recent_activity_previews_three_items_and_offers_show_older(
    tmp_path,
):
    db = Database(tmp_path / "activity-preview.db")
    await db.initialize()
    service = AppService(db, NoBrowser(), Pipeline(db))
    async with db.sessions.begin() as session:
        for index in range(5):
            session.add(Run(status="succeeded", stats={"found": index}))
            session.add(Listing(
                natural_key=f"listing-{index}",
                provider="qasa",
                external_id=str(index),
                url=f"https://qasa.com/home/{index}",
                stage="accepted",
                data={"address": f"Home {index}"},
                content_hash=f"hash-{index}",
            ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert "Show older checks" in response.text
    assert "Show older homes" in response.text
    assert response.text.count("data-older-item") == 4
    await db.dispose()
