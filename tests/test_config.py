import os

from qasawatch.config import load_env_file


def test_load_env_file_reads_values_without_overwriting_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "QASAWATCH_DISCORD_WEBHOOK_URL=https://discord.example/hook\n"
        "QASAWATCH_EXISTING=file-value\n"
        "QUOTED_VALUE=\"value with spaces\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QASAWATCH_EXISTING", "process-value")
    monkeypatch.delenv("QASAWATCH_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("QUOTED_VALUE", raising=False)

    assert load_env_file(env_file)
    assert os.environ["QASAWATCH_DISCORD_WEBHOOK_URL"] == "https://discord.example/hook"
    assert os.environ["QASAWATCH_EXISTING"] == "process-value"
    assert os.environ["QUOTED_VALUE"] == "value with spaces"


def test_process_environment_wins_for_every_dashboard_connection(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    names = (
        "QASAWATCH_GOOGLE_MAPS_API_KEY",
        "QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON",
        "QASAWATCH_DISCORD_WEBHOOK_URL",
        "QASAWATCH_SMTP_PASSWORD",
    )
    env_file.write_text(
        "\n".join(f"{name}=file-{index}" for index, name in enumerate(names)),
        encoding="utf-8",
    )
    for index, name in enumerate(names):
        monkeypatch.setenv(name, f"process-{index}")

    assert load_env_file(env_file)
    assert {
        name: os.environ[name]
        for name in names
    } == {
        name: f"process-{index}"
        for index, name in enumerate(names)
    }
