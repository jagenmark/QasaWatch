"""Resilient Qasa HTML/captured-data extraction with field provenance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlparse

from .domain import RawListing


_LISTING_PATH = re.compile(r"/(?:se/(?:sv|en)/)?home/([^/?#]+)", re.I)
_MONEY = re.compile(r"([\d\s.,]+)\s*(?:kr|sek)", re.I)
_ROOMS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:rum|rooms?)", re.I)
_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|kvm)", re.I)


@dataclass(frozen=True, slots=True)
class FieldValue:
    value: Any
    provenance: str


@dataclass(slots=True)
class ParsedListing:
    url: str
    external_id: str | None = None
    address: str | None = None
    rent: int | None = None
    rooms: float | None = None
    area: float | None = None
    availability: str | None = None
    rental_start: str | None = None
    rental_end: str | None = None
    duration: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_raw_listing(self) -> RawListing:
        data = {
            key: value for key, value in {
                "address": self.address, "rent": self.rent, "rooms": self.rooms,
                "area": self.area, "availability": self.availability,
                "rental_start": self.rental_start, "rental_end": self.rental_end,
                "duration": self.duration, "latitude": self.latitude,
                "longitude": self.longitude,
            }.items() if value is not None
        }
        data.update(self.attributes)
        data["provenance"] = dict(self.provenance)
        if self.errors:
            data["partial_errors"] = list(self.errors)
        return RawListing("qasa", self.url, self.external_id, data)


@dataclass(frozen=True, slots=True)
class ParsedPage:
    listings: tuple[ParsedListing, ...]
    explicit_empty: bool = False
    loading: bool = False
    auth_required: bool = False
    captcha: bool = False
    errors: tuple[str, ...] = ()


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str], str]] = []
        self.elements: list[dict[str, Any]] = []
        self.text_parts: list[str] = []
        self._script_attrs: dict[str, str] | None = None
        self._script_parts: list[str] = []
        self._open_tags: list[str] = []
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open_tags.append(tag.lower())
        values = {k.lower(): (v or "") for k, v in attrs}
        self.elements.append({"tag": tag.lower(), "attrs": values})
        if tag.lower() == "script":
            self._script_attrs, self._script_parts = values, []

    def handle_data(self, data: str) -> None:
        if self._script_attrs is not None:
            self._script_parts.append(data)
        else:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)
                if self._open_tags and self._open_tags[-1] == "title":
                    self.title_parts.append(stripped)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_attrs is not None:
            self.scripts.append((self._script_attrs, "".join(self._script_parts)))
            self._script_attrs = None
        lowered = tag.lower()
        for index in range(len(self._open_tags) - 1, -1, -1):
            if self._open_tags[index] == lowered:
                del self._open_tags[index:]
                break


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value).lower())
    folded = {normalize(k): v for k, v in item.items()}
    for key in keys:
        value = folded.get(normalize(key))
        if value not in (None, ""):
            return value
    return None


def _number(value: Any, *, integer: bool = False) -> float | int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if integer else float(value)
    if isinstance(value, str):
        match = re.search(r"\d[\d\s]*(?:[.,]\d+)?", value)
        if match:
            normalized = re.sub(r"\s+", "", match.group()).replace(",", ".")
            try:
                return int(float(normalized)) if integer else float(normalized)
            except ValueError:
                return None
    return None


def _candidate(mapping: Mapping[str, Any], base_url: str, source: str) -> ParsedListing | None:
    raw_url = _first(mapping, "url", "canonicalUrl", "listingUrl", "href")
    raw_id = _first(mapping, "id", "listingId", "homeId", "externalId", "identifier")
    kind = str(_first(mapping, "@type", "type", "__typename") or "").lower()
    if not raw_url and not raw_id:
        return None
    # Qasa nests HomeDocumentLocationType and HomeDocumentUploadType objects
    # below each actual HomeDocument.  Those children also have numeric IDs,
    # so a broad ``"home" in __typename`` check creates phantom listings.
    listing_kind = (
        any(word in kind for word in ("offer", "accommodation", "listing"))
        or kind in {"home", "homedocument", "apartment", "house", "residence"}
    )
    listing_fields = any(_first(mapping, key) is not None for key in
                         ("monthlyRent", "monthlyCost", "roomCount", "squareMeters", "listingId", "homeId"))
    if not raw_url and not listing_kind and not listing_fields:
        return None
    url = urljoin(base_url, str(raw_url)) if raw_url else urljoin(base_url, f"/home/{raw_id}")
    parsed_url = urlparse(url)
    hostname = (parsed_url.hostname or "").rstrip(".").lower()
    path_id = _LISTING_PATH.search(parsed_url.path)

    # Captured payloads also contain image, user and analytics objects with
    # numeric IDs and URLs. A listing must either have a canonical Qasa home
    # URL, or expose listing-specific fields/type from which one can be built.
    on_qasa = hostname == "qasa.com" or hostname.endswith(".qasa.com")
    if path_id and on_qasa:
        external_id = str(raw_id or path_id.group(1))
    elif raw_id and (listing_kind or listing_fields):
        external_id = str(raw_id)
        url = urljoin("https://qasa.com", f"/home/{external_id}")
    else:
        return None
    listing = ParsedListing(url=url, external_id=external_id)
    aliases = {
        "address": ("address", "streetAddress", "name", "title"),
        # Qasa's monthlyCost includes its service fee and is the actual amount
        # shown to the tenant; retain the lower base rent separately below.
        "rent": ("monthlyCost", "monthlyRent", "price", "rent"),
        "rooms": ("rooms", "roomCount", "numberOfRooms"),
        "area": ("area", "squareMeters", "floorSize"),
        "availability": ("availability", "status"),
        "rental_start": ("rentalStart", "availableFrom", "startDate"),
        "rental_end": ("rentalEnd", "availableTo", "endDate"),
        "duration": ("duration", "rentalLength", "rentalLengthSeconds", "leaseLength"),
        "latitude": ("latitude", "lat"), "longitude": ("longitude", "lng", "lon"),
    }
    for field_name, keys in aliases.items():
        value = _first(mapping, *keys)
        if isinstance(value, Mapping):
            value = _first(value, "value", "name", "streetAddress")
        if value is None:
            continue
        if field_name == "rent": value = _number(value, integer=True)
        elif field_name in ("rooms", "area", "latitude", "longitude"): value = _number(value)
        elif field_name == "duration" and isinstance(value, (int, float)):
            value = f"{round(value / 86_400)} days"
        if value is not None:
            setattr(listing, field_name, value)
            listing.provenance[field_name] = source
    location = _first(mapping, "location")
    if listing.address is None and isinstance(location, Mapping):
        address = " ".join(
            str(value).strip()
            for value in (_first(location, "route"), _first(location, "streetNumber"))
            if value not in (None, "")
        )
        locality = _first(location, "locality", "city")
        if locality:
            address = f"{address}, {locality}" if address else str(locality)
        if address:
            listing.address = address
            listing.provenance["address"] = source
    geo = _first(mapping, "geo", "coordinates", "location")
    if isinstance(geo, Mapping) and isinstance(_first(geo, "point"), Mapping):
        geo = _first(geo, "point")
    if isinstance(geo, Mapping):
        for field_name, keys in (("latitude", ("latitude", "lat")),
                                 ("longitude", ("longitude", "lng", "lon"))):
            if getattr(listing, field_name) is None and (value := _number(_first(geo, *keys))) is not None:
                setattr(listing, field_name, value); listing.provenance[field_name] = source
    listing.provenance.update({"url": source, "external_id": source})
    attribute_aliases = {
        "housing_type": ("housingType", "homeType"),
        "furnished": ("furnished",),
        "shared": ("shared", "sharedHome"),
        "pets_allowed": ("petsAllowed",),
        "smoking_allowed": ("smokingAllowed",),
        "wheelchair_accessible": ("wheelchairAccessible",),
        "first_hand": ("firstHand",),
        "student_home": ("studentHome",),
        "senior_home": ("seniorHome",),
        "instant_sign": ("instantSign",),
        "corporate_home": ("corporateHome",),
        "description": ("description",),
        "floor": ("floor",),
        "bedroom_count": ("bedroomCount",),
        "household_size": ("householdSize",),
        "published_at": ("publishedAt", "publishedOrBumpedAt"),
        "base_rent": ("rent",),
    }
    for key, keys in attribute_aliases.items():
        value = _first(mapping, *keys)
        if value is not None:
            if key == "furnished" and isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "yes", "1", "furnished", "möblerat"}:
                    value = True
                elif normalized in {"false", "no", "0", "unfurnished", "omöblerat"}:
                    value = False
            listing.attributes[key] = value
            listing.provenance[key] = source
    if listing.availability is None and listing.rental_start:
        listing.availability = (
            "fixed_period" if listing.rental_end else "until_further_notice"
        )
        listing.provenance["availability"] = "derived"
    if listing.duration is None and listing.rental_start and listing.rental_end:
        try:
            days = (
                date.fromisoformat(str(listing.rental_end)[:10])
                - date.fromisoformat(str(listing.rental_start)[:10])
            ).days
        except ValueError:
            pass
        else:
            listing.duration = f"{days} days"
            listing.provenance["duration"] = "derived"
    return listing


def parse_qasa_html(
    html: str,
    *,
    base_url: str = "https://qasa.com",
    captured_json: Iterable[Any] = (),
    results_only: bool = False,
) -> ParsedPage:
    doc = _Document()
    try:
        doc.feed(html)
    except Exception as exc:
        return ParsedPage((), errors=(f"malformed HTML: {exc}",))
    text = " ".join(doc.text_parts)
    lower = text.lower()
    found: list[ParsedListing] = []
    errors: list[str] = []
    for captured in captured_json:
        if results_only:
            for mapping in _home_search_nodes(captured):
                if item := _candidate(mapping, base_url, "captured-json"):
                    found.append(item)
            continue
        payload = (
            captured.get("payload")
            if isinstance(captured, Mapping) and "__qasawatch_operation" in captured
            else captured
        )
        for mapping in _walk(payload):
            if item := _candidate(mapping, base_url, "captured-json"):
                found.append(item)
    for attrs, body in (() if results_only else doc.scripts):
        script_type, script_id = attrs.get("type", ""), attrs.get("id", "")
        if "ld+json" not in script_type and script_id != "__NEXT_DATA__" and "application/json" not in script_type:
            continue
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            label = "next-data" if script_id == "__NEXT_DATA__" else script_id or script_type or "JSON"
            errors.append(f"invalid {label}: {exc.msg}")
            continue
        source = "json-ld" if "ld+json" in script_type else "next-data" if script_id == "__NEXT_DATA__" else "embedded-json"
        for mapping in _walk(payload):
            item = _candidate(mapping, base_url, source)
            if item:
                found.append(item)
    # Semantic links/data attributes survive CSS class churn.
    for element in (() if results_only else doc.elements):
        attrs = element["attrs"]
        href = attrs.get("href")
        listing_id = attrs.get("data-listing-id") or attrs.get("data-home-id")
        if (href and _LISTING_PATH.search(urlparse(urljoin(base_url, href)).path)) or listing_id:
            mapping: dict[str, Any] = {"url": href, "listingId": listing_id}
            mapping.update({k[5:]: v for k, v in attrs.items() if k.startswith("data-")})
            item = _candidate(mapping, base_url, "semantic-dom")
            if item:
                label = attrs.get("aria-label")
                if label:
                    _fill_text(item, label, "accessibility")
                found.append(item)
    # Merge duplicates by stable id first, then canonical URL, preferring earlier hierarchy.
    merged: list[ParsedListing] = []
    by_id: dict[str, ParsedListing] = {}
    by_url: dict[str, ParsedListing] = {}
    for item in found:
        canonical_url = item.url.rstrip("/")
        target = (by_id.get(item.external_id) if item.external_id else None) or by_url.get(canonical_url)
        if target is None:
            merged.append(item)
            if item.external_id:
                by_id[item.external_id] = item
            by_url[canonical_url] = item
            continue
        for name in ("address", "rent", "rooms", "area", "availability", "rental_start", "rental_end", "duration", "latitude", "longitude"):
            if getattr(target, name) is None and getattr(item, name) is not None:
                setattr(target, name, getattr(item, name)); target.provenance[name] = item.provenance[name]
        target.attributes.update({k: v for k, v in item.attributes.items() if k not in target.attributes})
    page_listing = _LISTING_PATH.search(urlparse(base_url).path)
    detail_target = (
        by_id.get(page_listing.group(1)) if page_listing else None
    ) or (merged[0] if len(merged) == 1 else None)
    if detail_target is not None:
        _fill_text(detail_target, text, "text")
        _fill_detail_text(detail_target, text, " ".join(doc.title_parts))
    explicit_empty = any(term in lower for term in ("inga bostäder matchar", "inga sökresultat", "no homes found", "0 bostäder"))
    loading = any(term in lower for term in ("laddar", "loading results", "hämtar bostäder"))
    auth = any(term in lower for term in ("logga in för att", "sign in to continue", "sessionen har gått ut"))
    captcha = any(term in lower for term in ("captcha", "verify you are human", "kontrollera att du är en människa"))
    return ParsedPage(tuple(merged), explicit_empty, loading, auth, captcha, tuple(errors))


def _home_search_nodes(captured: Any) -> tuple[Mapping[str, Any], ...]:
    """Return only the authoritative watcher result collection."""

    operation = None
    payload = captured
    if isinstance(captured, Mapping) and "__qasawatch_operation" in captured:
        operation = captured.get("__qasawatch_operation")
        payload = captured.get("payload")
    if operation not in (None, "HomeSearch") or not isinstance(payload, Mapping):
        return ()
    try:
        nodes = payload["data"]["homeIndexSearch"]["documents"]["nodes"]
    except (KeyError, TypeError):
        return ()
    if not isinstance(nodes, list):
        return ()
    return tuple(
        node
        for node in nodes
        if isinstance(node, Mapping) and node.get("__typename") == "HomeDocument"
    )


def _fill_text(listing: ParsedListing, text: str, source: str) -> None:
    for name, regex, integer in (("rent", _MONEY, True), ("rooms", _ROOMS, False), ("area", _AREA, False)):
        if getattr(listing, name) is None and (match := regex.search(text)):
            value = _number(match.group(1), integer=integer)
            if value is not None:
                setattr(listing, name, value)
                listing.provenance[name] = source
    if listing.address is None:
        parts = [part.strip() for part in re.split(r"[|·,]", text)]
        if parts and not _MONEY.search(parts[0]):
            listing.address = parts[0]; listing.provenance["address"] = source


def _fill_detail_text(listing: ParsedListing, text: str, title: str) -> None:
    """Last-resort semantic label extraction for an individual listing page."""

    if title and (
        listing.address is None
        or listing.address.lower().startswith("hoppa ")
        or listing.provenance.get("address") == "text"
    ):
        address = re.split(r"\s+-\s+[^|]+(?:\|.*)?$", title, maxsplit=1)[0].strip()
        if address:
            listing.address = address
            listing.provenance["address"] = "document-title"

    period = re.search(
        r"(?:Hyresperiod|Rental period)\s+"
        r"(?:(\d{4}-\d{2}-\d{2})\s+)?"
        r"(?:[-–—]\s*)?"
        r"(\d{4}-\d{2}-\d{2}|Tillsvidare|Until further notice)",
        text,
        re.I,
    )
    if period:
        if listing.rental_start is None and period.group(1):
            listing.rental_start = period.group(1)
            listing.provenance["rental_start"] = "semantic-text"
        open_ended = period.group(2).lower() in {
            "tillsvidare",
            "until further notice",
        }
        if listing.rental_end is None and not open_ended:
            listing.rental_end = period.group(2)
            listing.provenance["rental_end"] = "semantic-text"
        if open_ended and listing.availability is None:
            listing.availability = "until_further_notice"
            listing.provenance["availability"] = "semantic-text"
        if listing.duration is None and period.group(1) and not open_ended:
            try:
                days = (date.fromisoformat(period.group(2)) - date.fromisoformat(period.group(1))).days
                listing.duration = f"{days} days"
                listing.provenance["duration"] = "derived"
            except ValueError:
                pass

    move_in = re.search(
        r"(?:Inflyttningsdatum|Move-in date|Available from)\s+(\d{4}-\d{2}-\d{2})",
        text,
        re.I,
    )
    if move_in and listing.rental_start is None:
        listing.rental_start = move_in.group(1)
        listing.provenance["rental_start"] = "semantic-text"

    furnished: bool | None = None
    if re.search(r"\b(?:Omöblerat|Unfurnished)\b", text, re.I):
        furnished = False
    elif re.search(r"\b(?:Möblerat|Furnished)\b", text, re.I):
        furnished = True
    if furnished is not None and "furnished" not in listing.attributes:
        listing.attributes["furnished"] = furnished
        listing.provenance.setdefault("furnished", "semantic-text")
    if "Möjlighet till förlängning" in text:
        listing.availability = listing.availability or "extension_possible"
        listing.provenance.setdefault("availability", "semantic-text")

    monthly_cost = re.search(
        r"Månadskostnad\s+([\d\s.,]+)\s*(?:kr|SEK)", text, re.I
    )
    base_rent = re.search(
        r"Månadskostnad\s+[\d\s.,]+\s*(?:kr|SEK)\s+Hyra\s+([\d\s.,]+)\s*(?:kr|SEK)",
        text,
        re.I,
    )
    for key, match in (("monthly_cost", monthly_cost), ("base_rent", base_rent)):
        if match and (value := _number(match.group(1), integer=True)) is not None:
            listing.attributes[key] = value
            listing.provenance[key] = "semantic-text"
    if "monthly_cost" in listing.attributes:
        listing.rent = listing.attributes["monthly_cost"]
        listing.provenance["rent"] = "semantic-text"
