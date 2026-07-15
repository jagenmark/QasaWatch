import httpx
import pytest
from pydantic import ValidationError

from qasawatch.app import create_app
from qasawatch.db import Database
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
