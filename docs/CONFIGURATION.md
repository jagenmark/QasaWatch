# Configuration and provider reference

`WatcherConfig` is stored in SQLite under `watcher.config`. Use the dashboard or `PUT /api/config` with JSON shaped like [config.example.json](../config.example.json). It is not a file-import format. The normal dashboard automatically uses the standard environment setting for each private connection; low-level `*_secret_ref` fields remain API compatibility controls only.

## Core fields

| Field | Meaning |
| --- | --- |
| `enabled` | Enables scheduled scans. Commute destinations are optional. |
| `safe_mode` | Blocks production outputs, promotion/retry delivery, grouped-batch resend, and test email. Watcher scans still parse, enrich, filter, and report run counts, but do not create watcher listings, deduplication history, or delivery records. A listing first seen in safe mode remains new when a later production scan sees it. Manual detail inspection keeps its separate review history. |
| `qasa_results_url` | Exact HTTPS Qasa results URL. The supplied filtered URL is in the example config. |
| `base_interval_minutes` / `jitter_minutes` | Cadence of 1-1440 minutes plus/minus 0-120 minutes. |
| `destinations` | Optional list of any length. Each item has `label`, `address`, `commute_mode` (`arrival`/`departure`), and optional `maximum_commute_minutes`. |
| `filters` | Rent, rooms, area, commute, locations, keywords, availability, demographic limits, and tri-state listing-attribute requirements. |
| `maps_api_secret_ref` | `env:` reference for the shared Google Maps key. Geocoding supports SCB matching; Routes is used only for configured commute destinations. |

Databases upgraded from an older QasaWatch release may contain delivery rows
marked `skipped` by the previous safe-mode behavior. Those legacy rows remain
suppressed intentionally; the upgrade does not auto-send old notifications.
Requeue them only through an explicit, reviewed retry or manual promotion.

Each watcher scan opens one temporary tab for the configured rendered results page, captures its first-party HomeSearch response, parses only actual `HomeDocument` listings, and closes the tab. It does not open every listing. Manual URL inspection still renders the submitted detail page. Commutes use the next strict future weekday at 08:00 in `Europe/Stockholm`. Arrival destinations get an 08:00 arrival time; departure destinations get an 08:00 departure time.

The dashboard provides ordinary controls for destinations, filters, listing
attributes, Sheets, Discord, SMTP email, and the main SCB dataset fields.
Enter station/address and recipient text directly. A collapsed **Advanced JSON
controls** section remains available for bulk editing, unusual SCB mappings, or
new fields that do not yet have a dedicated control; it is optional. Values in
the ordinary controls take precedence. Configuration errors return to the
dashboard with a readable message and do not overwrite the last valid settings.

`filters.attribute_requirements` maps a supported boolean attribute to `true` or `false`. The dashboard exposes Ignore / Require yes / Require no controls for furnished, shared, pets, smoking, wheelchair access, first-hand, student/senior, instant-sign, and corporate homes. An enabled requirement rejects a missing value and records an explicit reason.

## Google Maps

Enable Geocoding API and Routes API in a dedicated Google Cloud project. Use a server key, restrict it to those APIs and, where egress is stable, the VM source IP. Apply quotas and budget alerts. Put the key in `QASAWATCH_GOOGLE_MAPS_API_KEY`; the normal dashboard selects it automatically.

The runtime calls Geocoding and Routes Compute Route Matrix as needed. Do not put the key in URLs, logs, Sheets, or Discord. Google-derived storage/caching must comply with your agreement; unavailable routes/geocodes produce status diagnostics.

## Google Sheets

Enable Google Sheets API, create a least-privilege service account, and share the exact spreadsheet with its service-account email. Configure `spreadsheet_id`, `worksheet`, and `credentials_secret_ref`.

The concrete Sheets v4 client accepts the resolved secret as either compact service-account JSON or an absolute path to a protected service-account key file. It uses `google-auth` with the Sheets scope, checks column A for the stable idempotency key, then appends a 15-column summary row. Store key files outside the checkout with restrictive ownership/mode.

## Discord

Create a webhook for a least-privilege channel. Store the URL in `QASAWATCH_DISCORD_WEBHOOK_URL`; the normal dashboard selects it automatically. The runtime sends a Swedish listing summary with rent, area, address, rooms, rental period, commute bullets, and available demographics. Mentions are disabled and the request carries an idempotency header. Keep safe mode on until the channel is verified.

## Environment precedence

At startup, an existing process or VM environment variable wins. The project
`.env` file fills only missing values. The dashboard never overrides or
redisplays private values. After changing either source, restart QasaWatch and
use the dashboard connection test; a saved configuration flag alone is not
proof that an external service accepts the credential.

When available, the Discord summary also shows `Möblerat`/`Omöblerat`, a
separate `Inflyttningsdatum`, and `Tillsvidare` for an open-ended rental period.

## SMTP email

Configure `smtp_host`, `smtp_port`, valid `sender`, `recipients`, optional `smtp_username`, and `smtp_secret_ref` for the password. If username is omitted, the runtime uses the sender as the SMTP username. Modes are `starttls` (normally 587), implicit `tls` (normally 465), and `plain` only for a trusted local relay. Username and password are required together by the transport.

For grouped mail set `grouped: true` and `per_listing: false`; it creates one durable scan batch. For per-listing mail set `grouped: false` and `per_listing: true`; the runtime selects mode from `grouped`, so keep the two fields consistent. `send_no_new: true` permits an empty grouped notice. `POST /api/test-email` sends only when safe mode is off. Listing retry uses `POST /api/retry` with `channels: ["email"]`; grouped batches use `POST /api/email-batches/{batch_id}/retry`.

To test email from the dashboard, save valid SMTP settings, enable email,
temporarily turn off safe mode, and press **Send test email** in the Email
section. The result is displayed there. The test action is explicit and does
not add a listing or affect watcher deduplication.

Explicitly promoting or retrying one listing to email always sends one listing message. It does not wait for, or join, a grouped watcher scan.

An SMTP failure after send starts is ambiguous and enters manual review to avoid duplicate mail. Confirm the recipient outcome, then use `POST /api/email-batches/{batch_id}/resolve?delivered=true|false`. Disable delivery with `email.enabled: false` and enable safe mode while editing relay or recipients.

## SCB GeoJSON

SCB is optional. Configure `scb.data_path`, `id_column`, `name_column`, `demographic_mapping`, `vintage`, and `crs` (default `EPSG:4326`). `crs` is an operator declaration; the loader accepts only a GeoJSON `FeatureCollection` whose own metadata declares source, vintage, and one of `EPSG:4326`, `CRS84`, or `OGC:CRS84`:

```json
{"source":"SCB", "vintage":"2025", "crs":"EPSG:4326"}
```

Each feature needs the configured ID/name properties and Polygon or MultiPolygon geometry. `demographic_mapping` maps output names to source properties; `municipality_mapping` is retained configuration for local municipality-code interpretation. QasaWatch never transforms CRS or silently treats projected coordinates as WGS84: convert data before loading.

An absent or invalid configured dataset becomes an unavailable partial SCB result and does not abort a listing scan. Pin the expected `vintage`, record source/vintage/checksum with deployment, and test a known point after a refresh.

For the Stockholm-only local dataset used by the default deployment, run:

```sh
uv run python scripts/build_stockholm_scb.py
```

The builder downloads SCB's DeSO 2025 WFS polygons for Stockholm County (`lanskod` `01`) and joins SCB table `FolkmDesoBakgrKon` (`TAB6571`) for reference year 2025. This covers Stockholm municipality and surrounding municipalities such as Solna, Sundbyberg and Sollentuna. It stores total population, foreign-background count and calculated share, area level, precision and source metadata in `data/scb/stockholm-deso-2025.geojson`. SCB applies Cell Key Method uncertainty to 2025 values, so the percentage is labelled approximate. Re-run the builder deliberately when adopting a newer compatible geography/statistics vintage, then update and verify `scb.vintage`.

For nationwide coverage across all 21 counties, run:

```sh
uv run python scripts/build_sweden_scb.py
```

The national builder uses the same 2025 geography and statistics sources,
batches the PxWeb requests, validates that all 21 counties are present, and
writes `data/scb/sweden-deso-2025.geojson`. It uses the same dashboard field
mapping as the Stockholm file.
