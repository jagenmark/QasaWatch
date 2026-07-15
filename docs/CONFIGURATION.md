# Configuration and provider reference

`WatcherConfig` is stored in SQLite under `watcher.config`. Use the dashboard or `PUT /api/config` with JSON shaped like [config.example.json](../config.example.json). It is not a file-import format. The dashboard preserves existing secret references; use the API for initial or changed `maps_api_secret_ref` values.

## Core fields

| Field | Meaning |
| --- | --- |
| `enabled` | Enables scheduled scans. Two or more destinations are required when true. |
| `safe_mode` | Blocks production outputs, promotion/retry delivery, grouped-batch resend, and test email. It does not disable results parsing, manual detail inspection, Maps, or local SCB enrichment. Enabled per-listing channels are durably marked skipped for newly accepted safe-mode results, so switching safe mode off cannot backfill old test discoveries. |
| `qasa_results_url` | Exact HTTPS Qasa results URL. The supplied filtered URL is in the example config. |
| `base_interval_minutes` / `jitter_minutes` | Cadence of 1-1440 minutes plus/minus 0-120 minutes. |
| `destinations` | `label`, `address`, `commute_mode` (`arrival`/`departure`), and optional `maximum_commute_minutes`. |
| `filters` | Rent, rooms, area, commute, locations, keywords, availability, demographic limits, and tri-state listing-attribute requirements. |
| `maps_api_secret_ref` | `env:` reference for the Google Maps key; Maps is used only when it and destinations are configured. |

Each watcher scan opens one temporary tab for the configured rendered results page, captures its first-party HomeSearch response, parses only actual `HomeDocument` listings, and closes the tab. It does not open every listing. Manual URL inspection still renders the submitted detail page. Commutes use the next strict future weekday at 08:00 in `Europe/Stockholm`. Arrival destinations get an 08:00 arrival time; departure destinations get an 08:00 departure time.

`filters.attribute_requirements` maps a supported boolean attribute to `true` or `false`. The dashboard exposes Ignore / Require yes / Require no controls for furnished, shared, pets, smoking, wheelchair access, first-hand, student/senior, instant-sign, and corporate homes. An enabled requirement rejects a missing value and records an explicit reason.

## Google Maps

Enable Geocoding API and Routes API in a dedicated Google Cloud project. Use a server key, restrict it to those APIs and, where egress is stable, the VM source IP. Apply quotas and budget alerts. Put the key in `QASAWATCH_GOOGLE_MAPS_API_KEY` and store only `env:QASAWATCH_GOOGLE_MAPS_API_KEY` in `maps_api_secret_ref`.

The runtime calls Geocoding and Routes Compute Route Matrix as needed. Do not put the key in URLs, logs, Sheets, or Discord. Google-derived storage/caching must comply with your agreement; unavailable routes/geocodes produce status diagnostics.

## Google Sheets

Enable Google Sheets API, create a least-privilege service account, and share the exact spreadsheet with its service-account email. Configure `spreadsheet_id`, `worksheet`, and `credentials_secret_ref`.

The concrete Sheets v4 client accepts the resolved secret as either compact service-account JSON or an absolute path to a protected service-account key file. It uses `google-auth` with the Sheets scope, checks column A for the stable idempotency key, then appends a 15-column summary row. Store key files outside the checkout with restrictive ownership/mode.

## Discord

Create a webhook for a least-privilege channel. Store the URL in `QASAWATCH_DISCORD_WEBHOOK_URL` and persist only `env:QASAWATCH_DISCORD_WEBHOOK_URL` in `webhook_secret_ref`. The runtime sends a Swedish listing summary with rent, area, address, rooms, rental period, commute bullets, and available demographics. Mentions are disabled and the request carries an idempotency header. Keep safe mode on until the channel is verified.

## SMTP email

Configure `smtp_host`, `smtp_port`, valid `sender`, `recipients`, optional `smtp_username`, and `smtp_secret_ref` for the password. If username is omitted, the runtime uses the sender as the SMTP username. Modes are `starttls` (normally 587), implicit `tls` (normally 465), and `plain` only for a trusted local relay. Username and password are required together by the transport.

For grouped mail set `grouped: true` and `per_listing: false`; it creates one durable scan batch. For per-listing mail set `grouped: false` and `per_listing: true`; the runtime selects mode from `grouped`, so keep the two fields consistent. `send_no_new: true` permits an empty grouped notice. `POST /api/test-email` sends only when safe mode is off. Listing retry uses `POST /api/retry` with `channels: ["email"]`; grouped batches use `POST /api/email-batches/{batch_id}/retry`.

Explicitly promoting or retrying one listing to email always sends one listing message. It does not wait for, or join, a grouped watcher scan. An explicit promotion/retry after safe mode is disabled resets the tombstone only for its selected channels; automatic scans never do this.

An SMTP failure after send starts is ambiguous and enters manual review to avoid duplicate mail. Confirm the recipient outcome, then use `POST /api/email-batches/{batch_id}/resolve?delivered=true|false`. Disable delivery with `email.enabled: false` and enable safe mode while editing relay or recipients.

## SCB GeoJSON

SCB is optional. Configure `scb.data_path`, `id_column`, `name_column`, `demographic_mapping`, `vintage`, and `crs` (default `EPSG:4326`). `crs` is an operator declaration; the loader accepts only a GeoJSON `FeatureCollection` whose own metadata declares source, vintage, and one of `EPSG:4326`, `CRS84`, or `OGC:CRS84`:

```json
{"source":"SCB", "vintage":"2025", "crs":"EPSG:4326"}
```

Each feature needs the configured ID/name properties and Polygon or MultiPolygon geometry. `demographic_mapping` maps output names to source properties; `municipality_mapping` is retained configuration for local municipality-code interpretation. QasaWatch never transforms CRS or silently treats projected coordinates as WGS84: convert data before loading.

An absent or invalid configured dataset becomes an unavailable partial SCB result and does not abort a listing scan. Pin the expected `vintage`, record source/vintage/checksum with deployment, and test a known point after a refresh.
