import json
from dataclasses import asdict, replace

import pytest

import qasawatch.browser_host as browser_host
from qasawatch.browser import BrowserScan, QasaDetailEnricher, validate_qasa_url
from qasawatch.browser_host import (
    BrowserDescriptor,
    BrowserHostError,
    ChromeHost,
    find_chrome,
)
from qasawatch.domain import RawListing
from qasawatch.parser import ParsedListing, ParsedPage
from qasawatch.readiness import ReadinessResult, ReadinessState


@pytest.mark.parametrize("url", [
    "https://qasa.com/se/sv/find-home?maxRoomCount=3",
    "https://www.qasa.com/se/sv/home/example",
])
def test_qasa_url_allowlist(url):
    assert validate_qasa_url(url) == url


@pytest.mark.parametrize("url", [
    "http://qasa.com/home/1", "https://qasa.com.evil.test/home/1",
    "https://evil.test/?next=qasa.com", "https://user@qasa.com/home/1",
])
def test_qasa_url_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        validate_qasa_url(url)


def test_descriptor_read_and_process_ownership_requires_all_markers(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(42, 9222, "secret", "/opt/chrome", str((tmp_path / "chrome-profile").resolve()), 1.0)
    host.descriptor_path.write_text(json.dumps(asdict(descriptor)), "utf-8")
    assert host.read_descriptor() == descriptor
    command = (
        f"/opt/chrome --user-data-dir={descriptor.profile_dir} "
        "--qasawatch-owner=secret --remote-debugging-port=9222"
    )
    monkeypatch.setattr("qasawatch.browser_host._process_command_line", lambda pid: command)
    assert host.owns_process(descriptor)
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line",
        lambda pid: (
            f"/opt/chrome --user-data-dir={descriptor.profile_dir} "
            "--qasawatch-owner=wrong --remote-debugging-port=9222"
        ),
    )
    assert not host.owns_process(descriptor)


def test_process_ownership_survives_linux_wrapper_handoff(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(
        42,
        9222,
        "secret",
        "/usr/bin/google-chrome",
        str((tmp_path / "chrome-profile").resolve()),
        1.0,
    )
    command = (
        f"/opt/google/chrome/chrome --remote-debugging-port=9222 "
        f"--user-data-dir={descriptor.profile_dir} --qasawatch-owner=secret"
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: command
    )

    assert host.owns_process(descriptor)


@pytest.mark.parametrize(
    "command",
    [
        (
            '/opt/google/chrome/chrome --remote-debugging-port=9222 '
            '"--user-data-dir=/tmp/profile with spaces" '
            "--qasawatch-owner=secret"
        ),
        (
            "/opt/google/chrome/chrome --remote-debugging-port=9222 "
            "--user-data-dir=/tmp/profile with spaces "
            "--qasawatch-owner=secret"
        ),
    ],
)
def test_process_ownership_accepts_quoted_and_procfs_arguments(tmp_path, monkeypatch, command):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(
        42, 9222, "secret", "/usr/bin/google-chrome", "/tmp/profile with spaces", 1.0
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: command
    )

    assert host.owns_process(descriptor)


def test_process_ownership_rejects_marker_prefixes(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(
        42, 9222, "secret", "/usr/bin/google-chrome", "/tmp/profile", 1.0
    )
    command = (
        "/opt/google/chrome/chrome --remote-debugging-port=92220 "
        "--user-data-dir=/tmp/profile-extra --qasawatch-owner=secret-extra"
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: command
    )

    assert not host.owns_process(descriptor)


def test_process_ownership_matches_exact_procfs_arguments(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(
        42, 9222, "secret", "/usr/bin/google-chrome", "/tmp/profile with spaces", 1.0
    )
    command = (
        "/opt/google/chrome/chrome",
        "--remote-debugging-port=9222",
        "--user-data-dir=/tmp/profile with spaces",
        "--qasawatch-owner=secret",
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: command
    )

    assert host.owns_process(descriptor)


def test_find_chrome_honors_explicit_executable(tmp_path, monkeypatch):
    chrome = tmp_path / "custom chrome"
    chrome.touch()
    monkeypatch.setenv("QASAWATCH_CHROME_EXECUTABLE", str(chrome))

    assert find_chrome() == chrome.resolve()


def test_find_chrome_rejects_missing_explicit_executable(tmp_path, monkeypatch):
    missing = tmp_path / "missing-chrome"
    monkeypatch.setenv("QASAWATCH_CHROME_EXECUTABLE", str(missing))

    with pytest.raises(BrowserHostError, match="does not point to a file"):
        find_chrome()


def test_linux_startup_requires_graphical_display(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.touch()
    host = ChromeHost(tmp_path / "state", executable=chrome, port=9222)
    monkeypatch.setattr("qasawatch.browser_host.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    with pytest.raises(BrowserHostError, match="graphical display"):
        host.start_or_adopt()

    host.close()


def test_linux_termination_tolerates_process_exit(monkeypatch):
    descriptor = BrowserDescriptor(
        42, 9222, "secret", "/usr/bin/google-chrome", "/tmp/profile", 1.0
    )
    monkeypatch.setattr("qasawatch.browser_host.os.name", "posix")

    def missing_process(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr("qasawatch.browser_host.os.kill", missing_process)

    ChromeHost._terminate_owned(descriptor)


def test_macos_process_lookup_uses_ps(monkeypatch):
    class Result:
        stdout = (
            "/opt/google/chrome/chrome --remote-debugging-port=9222 "
            "--user-data-dir=/tmp/profile --qasawatch-owner=secret\n"
        )

    monkeypatch.setattr(browser_host.os, "name", "posix")
    monkeypatch.setattr(browser_host.sys, "platform", "darwin")
    monkeypatch.setattr(browser_host.subprocess, "run", lambda *args, **kwargs: Result())

    command = browser_host._process_command_line(42)

    assert "--qasawatch-owner=secret" in command


def test_macos_process_enumeration_uses_ps(monkeypatch):
    class Result:
        stdout = (
            "  42 /opt/google/chrome/chrome --qasawatch-owner=secret\n"
            "invalid line\n"
        )

    monkeypatch.setattr(browser_host.os, "name", "posix")
    monkeypatch.setattr(browser_host.sys, "platform", "darwin")
    monkeypatch.setattr(browser_host.subprocess, "run", lambda *args, **kwargs: Result())

    commands = browser_host._iter_process_command_lines()

    assert commands == [
        (42, "/opt/google/chrome/chrome --qasawatch-owner=secret")
    ]


def test_descriptor_refreshes_after_chrome_pid_handoff(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(
        42,
        9222,
        "secret",
        "/opt/chrome",
        str((tmp_path / "chrome-profile").resolve()),
        1.0,
    )
    command = (
        f"/opt/chrome --user-data-dir={descriptor.profile_dir} "
        "--qasawatch-owner=secret --remote-debugging-port=9222"
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: None
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._iter_process_command_lines",
        lambda: [(99, command)],
    )

    refreshed = host._refresh_owned_descriptor(descriptor)

    assert refreshed is not None
    assert refreshed.pid == 99
    assert refreshed.port == descriptor.port


def test_start_or_adopt_uses_refreshed_handoff_pid(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    host.state_dir.mkdir(parents=True, exist_ok=True)
    host.profile_dir.mkdir()
    descriptor = BrowserDescriptor(
        42,
        9222,
        "secret",
        "/opt/chrome",
        str(host.profile_dir.resolve()),
        1.0,
    )
    host._write_descriptor(descriptor)
    command = (
        f"/opt/chrome --user-data-dir={descriptor.profile_dir} "
        "--qasawatch-owner=secret --remote-debugging-port=9222"
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: None
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._iter_process_command_lines",
        lambda: [(99, command)],
    )
    monkeypatch.setattr(host, "_cdp_healthy", lambda port: True)

    adopted = host.start_or_adopt()

    assert adopted.pid == 99
    assert host.read_descriptor().pid == 99
    host.close()


def test_descriptor_health_rejects_unowned_cdp_listener(tmp_path, monkeypatch):
    host = ChromeHost(tmp_path)
    descriptor = BrowserDescriptor(
        42,
        9222,
        "secret",
        "/opt/chrome",
        str((tmp_path / "chrome-profile").resolve()),
        1.0,
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._process_command_line", lambda pid: None
    )
    monkeypatch.setattr(
        "qasawatch.browser_host._iter_process_command_lines", lambda: []
    )
    monkeypatch.setattr(host, "_cdp_healthy", lambda port: True)

    assert not host.descriptor_healthy(descriptor)


def test_startup_waits_for_owned_pid_after_launcher_handoff(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome.exe"
    chrome.touch()
    host = ChromeHost(tmp_path / "state", executable=chrome, port=9222)

    class ExitedLauncher:
        pid = 42

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(
        "qasawatch.browser_host.subprocess.Popen", lambda *args, **kwargs: ExitedLauncher()
    )
    monkeypatch.setattr(host, "_cdp_healthy", lambda port: True)
    monkeypatch.setattr("qasawatch.browser_host.time.sleep", lambda seconds: None)
    calls = iter((None, None, "replacement"))

    def refresh(descriptor):
        value = next(calls)
        return replace(descriptor, pid=99) if value else None

    monkeypatch.setattr(host, "_refresh_owned_descriptor", refresh)

    adopted = host.start_or_adopt(timeout=1)

    assert adopted.pid == 99
    assert host.read_descriptor().pid == 99
    host.close()


@pytest.mark.asyncio
async def test_detail_enricher_merges_rendered_fields_and_provenance():
    class Browser:
        async def scan(self, url, *, timeout):
            item = ParsedListing(
                url=url,
                external_id="42",
                address="Rendered address",
                rooms=2,
                area=48,
                rental_start="2026-08-22",
                rental_end="2027-04-30",
                provenance={"address": "document-title"},
            )
            return BrowserScan(
                ParsedPage((item,)),
                ReadinessResult(ReadinessState.READY, "stable", ("42",)),
                url,
            )

    enriched = await QasaDetailEnricher(Browser()).enrich(
        RawListing("qasa", "https://qasa.com/home/42", "42", {"rent": 9500})
    )
    assert enriched.data["rent"] == 9500
    assert enriched.data["address"] == "Rendered address"
    assert enriched.data["rental_end"] == "2027-04-30"
    assert enriched.data["provenance"]["address"] == "document-title"
