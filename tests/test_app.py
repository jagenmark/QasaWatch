import httpx

from qasawatch.app import create_app
from qasawatch.db import Database
from qasawatch.pipeline import Pipeline
from qasawatch.schemas import WatcherConfig
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
