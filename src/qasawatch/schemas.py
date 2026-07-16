"""Validated operator-facing configuration and API payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from .browser import validate_qasa_url
from .secrets import SecretRef


ListingAttribute = Literal[
    "furnished",
    "shared",
    "pets_allowed",
    "smoking_allowed",
    "wheelchair_accessible",
    "first_hand",
    "student_home",
    "senior_home",
    "instant_sign",
    "corporate_home",
]


def _secret_reference(value: str | None) -> str | None:
    if value is not None:
        SecretRef.parse(value)
    return value


class Destination(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=1, max_length=300)
    commute_mode: Literal["arrival", "departure"] = "arrival"
    maximum_commute_minutes: int | None = Field(None, ge=1)


class FilterSettings(BaseModel):
    minimum_rent: int | None = Field(None, ge=0)
    maximum_rent: int | None = Field(None, ge=0)
    minimum_rooms: float | None = Field(None, ge=0)
    maximum_rooms: float | None = Field(None, ge=0)
    minimum_area: float | None = Field(None, ge=0)
    maximum_area: float | None = Field(None, ge=0)
    allowed_locations: list[str] = Field(default_factory=list)
    excluded_locations: list[str] = Field(default_factory=list)
    required_keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    availability_from: str | None = None
    availability_to: str | None = None
    minimum_population: int | None = Field(None, ge=0)
    maximum_population: int | None = Field(None, ge=0)
    maximum_average_age: float | None = Field(None, ge=0)
    minimum_foreign_background_percent: float | None = Field(
        None, ge=0, le=100
    )
    maximum_foreign_background_percent: float | None = Field(
        None, ge=0, le=100
    )
    attribute_requirements: dict[ListingAttribute, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered_ranges(self) -> "FilterSettings":
        if self.minimum_rent is not None and self.maximum_rent is not None and self.minimum_rent > self.maximum_rent:
            raise ValueError("minimum_rent cannot exceed maximum_rent")
        if self.minimum_rooms is not None and self.maximum_rooms is not None and self.minimum_rooms > self.maximum_rooms:
            raise ValueError("minimum_rooms cannot exceed maximum_rooms")
        if self.minimum_area is not None and self.maximum_area is not None and self.minimum_area > self.maximum_area:
            raise ValueError("minimum_area cannot exceed maximum_area")
        if (
            self.minimum_foreign_background_percent is not None
            and self.maximum_foreign_background_percent is not None
            and self.minimum_foreign_background_percent
            > self.maximum_foreign_background_percent
        ):
            raise ValueError(
                "minimum_foreign_background_percent cannot exceed "
                "maximum_foreign_background_percent"
            )
        return self


class SheetsSettings(BaseModel):
    enabled: bool = False
    spreadsheet_id: str = ""
    worksheet: str = "Listings"
    credentials_secret_ref: str | None = None

    _valid_secret = field_validator("credentials_secret_ref")(_secret_reference)


class DiscordSettings(BaseModel):
    enabled: bool = False
    webhook_secret_ref: str | None = None

    _valid_secret = field_validator("webhook_secret_ref")(_secret_reference)


class EmailSettings(BaseModel):
    enabled: bool = False
    recipients: list[str] = Field(default_factory=list)
    sender: str = ""
    smtp_mode: Literal["starttls", "tls", "plain"] = "starttls"
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_secret_ref: str | None = None
    grouped: bool = True
    per_listing: bool = False
    send_no_new: bool = False
    subject: str = "QasaWatch: {count} new listings"

    _valid_secret = field_validator("smtp_secret_ref")(_secret_reference)


class ScbSettings(BaseModel):
    data_path: str = ""
    municipality_mapping: dict[str, str] = Field(default_factory=dict)
    id_column: str = "municipality_id"
    name_column: str = "municipality_name"
    demographic_mapping: dict[str, str] = Field(default_factory=dict)
    vintage: str = ""
    crs: str = "EPSG:4326"


class WatcherConfig(BaseModel):
    enabled: bool = False
    qasa_results_url: str = "https://qasa.com/se/sv/find-home"
    max_result_pages: int = Field(5, ge=1, le=100)
    max_result_listings: int = Field(250, ge=1, le=5000)
    base_interval_minutes: int = Field(15, ge=1, le=1440)
    jitter_minutes: int = Field(3, ge=0, le=120)
    destinations: list[Destination] = Field(default_factory=list)
    filters: FilterSettings = Field(default_factory=FilterSettings)
    sheets: SheetsSettings = Field(default_factory=SheetsSettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    scb: ScbSettings = Field(default_factory=ScbSettings)
    safe_mode: bool = True
    maps_api_secret_ref: str | None = None

    _valid_maps_secret = field_validator("maps_api_secret_ref")(_secret_reference)

    @field_validator("qasa_results_url")
    @classmethod
    def valid_results_url(cls, value: str) -> str:
        return validate_qasa_url(value.strip())

class ManualRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def strict_qasa_listing(cls, value: str) -> str:
        value = validate_qasa_url(value.strip())
        import re
        from urllib.parse import urlparse
        path = urlparse(value).path.rstrip("/")
        if not re.search(r"/(?:home|homes|rental|rentals)/[^/]+$", path, re.I):
            raise ValueError("URL must be an individual Qasa home listing")
        return value


class PromotionRequest(BaseModel):
    manual_id: int = Field(ge=1)
    channels: list[Literal["sheets", "discord", "email"]] = Field(default_factory=list)


class RetryRequest(BaseModel):
    listing_id: int = Field(ge=1)
    channels: list[Literal["sheets", "discord", "email"]] = Field(default_factory=list)


class TestEmailRequest(BaseModel):
    recipient: str | None = None


def public_config(config: WatcherConfig) -> dict[str, Any]:
    """Return configuration safe for JSON or HTML; never expose secret references."""
    value = config.model_dump()
    value["maps_secret_configured"] = bool(value.pop("maps_api_secret_ref", None))
    for section, key in (("sheets", "credentials_secret_ref"), ("discord", "webhook_secret_ref"), ("email", "smtp_secret_ref")):
        reference = value[section].pop(key, None)
        value[section]["secret_configured"] = bool(reference)
    username = value["email"].pop("smtp_username", None)
    value["email"]["username_configured"] = bool(username)
    return value
