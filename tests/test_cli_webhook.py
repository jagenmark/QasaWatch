import json

import pytest

from qasawatch import cli


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"id": "message-1"}).encode()


@pytest.mark.asyncio
async def test_webhook_client_identifies_itself_to_discord(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = await cli._WebhookClient().post(
        "https://discord.test/webhook",
        {"content": "test"},
        headers={"Idempotency-Key": "delivery-1"},
    )

    assert result == {"id": "message-1"}
    assert captured["request"].get_header("User-agent").startswith("QasaWatch/")
    assert captured["request"].get_header("Idempotency-key") == "delivery-1"
    assert captured["request"].full_url.endswith("?wait=true")
    assert captured["timeout"] == 20
