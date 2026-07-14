import json
from dataclasses import asdict

import pytest

from qasawatch.browser import BrowserScan, QasaDetailEnricher, validate_qasa_url
from qasawatch.browser_host import BrowserDescriptor, ChromeHost
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
    command = f"/opt/chrome --user-data-dir={descriptor.profile_dir} --qasawatch-owner=secret"
    monkeypatch.setattr("qasawatch.browser_host._process_command_line", lambda pid: command)
    assert host.owns_process(descriptor)
    monkeypatch.setattr("qasawatch.browser_host._process_command_line", lambda pid: "/opt/chrome --qasawatch-owner=wrong")
    assert not host.owns_process(descriptor)


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
