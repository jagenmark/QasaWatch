# QasaWatch

QasaWatch is a restartable Qasa listing watcher. It controls a real installed Google Chrome through a loopback-only Chrome DevTools Protocol (CDP) port, stores durable state in SQLite/WAL, and quarantines unstable, login, CAPTCHA, and incomplete pages instead of treating them as empty results.

The packaged `qasawatch` command starts the dashboard and scheduler. It wires rendered Qasa results parsing, Google Maps commute enrichment, optional SCB GeoJSON, Google Sheets v4 service-account delivery, Discord webhooks, and generic SMTP from stored configuration and environment-secret references.

## What is implemented

- Durable SQLite configuration, scan leases, watcher/manual history, errors, delivery attempts, and grouped-email batches.
- Persistent dedicated Chrome with loopback CDP and Playwright-over-CDP jobs.
- One rendered results-page load per watcher scan; Qasa's HomeSearch data supplies the complete visible result cards without per-listing browser navigation.
- Detail-page rendering only for the explicit manual URL inspector.
- Next-weekday 08:00 Europe/Stockholm arrival/departure commutes, filtering, durable outputs, dashboard/API review, promotion, retries, and safe verification mode.

## Install

Requirements: Python 3.12+, current Google Chrome, and Qasa network access. Browser support needs the Playwright Python package; it attaches to installed Chrome. Google Sheets support uses the included `google-auth` dependency.

Windows (PowerShell):

```powershell
uv sync --all-extras
uv run pytest
```

Linux VM:

```sh
sudo apt-get update
sudo apt-get install -y google-chrome-stable xvfb openbox x11vnc
uv sync --all-extras
uv run pytest
```

With `pip` instead of `uv`:

```sh
python3 -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e '.[browser,test]'
pytest
```

Use a non-login `qasawatch` VM account and keep persistent state outside the checkout, such as `/var/lib/qasawatch`.

## First browser setup

The Chrome profile contains Qasa session cookies and may contain browser tokens. Give it mode `0700`, never commit or share it, and use a dedicated profile rather than a personal daily-use profile.

1. Start `qasawatch --database /var/lib/qasawatch/qasawatch.db`. Startup creates or adopts Chrome even while scheduled watching is disabled, using `/var/lib/qasawatch/.qasawatch/chrome-profile`; CDP is dynamically selected and loopback-only. Automation attaches on the first scan, independently of the real browser window. If Chrome cannot start, the dashboard remains available and reports the browser error.
2. Use the protected local graphical console or [remote-access pattern](docs/OPERATIONS.md#browser-access-on-a-headless-linux-vm) to sign in to Qasa in that exact profile. Complete CAPTCHA manually.
3. Set the desired Qasa filters in the profile and use the exact results URL in [config.example.json](config.example.json):

   ```text
   https://qasa.com/se/sv/find-home?ne_lat=59.395990187569225&ne_lng=18.166765064212683&sw_lat=59.29144514172344&sw_lng=17.958198610912206&maxRoomCount=3&maxMonthlyCost=10300&minSquareMeters=25&minRentalLength=15778476&sharedHome=privateHome
   ```

4. Start with `safe_mode: true`. Confirm the dashboard count matches the number of Qasa result cards before enabling production outputs; safe mode sends nothing.

If Chrome crashes, the host only replaces a process it can prove it owns by PID, executable, profile path, and owner token. Do not run another supervisor against the profile. Re-authenticate manually if Qasa signs the profile out.

## Configuration and secrets

[config.example.json](config.example.json) is the JSON shape accepted by `PUT /api/config`; it is not auto-loaded from disk. The dashboard stores the same configuration in SQLite. An enabled watcher requires at least two destinations.

Bootstrap database/log settings are read directly from environment. Integration secrets are stored as `env:VARIABLE_NAME` references and resolved only in the running process. See [`.env.example`](.env.example); protect the real environment file with `0600` permissions. The public API/dashboard redact secret references and SMTP username.

See [configuration](docs/CONFIGURATION.md) for provider fields and [operations](docs/OPERATIONS.md) for activation, backups, recovery, and secure browser access.

## Start and operate

```sh
qasawatch --database /var/lib/qasawatch/qasawatch.db --host 127.0.0.1 --port 8000
```

On startup QasaWatch initializes the database and calls `recover_interrupted_work()` before starting the scheduler. The scheduler uses Europe/Stockholm, persists next-run state, applies interval jitter, and uses a DB lease to prevent overlap. `POST /api/run-now` runs even when scheduled watching is disabled, so use safe mode for operational tests.

For Linux service operation use [deployment/systemd](deployment/systemd/):

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now qasawatch
sudo systemctl status qasawatch
sudo journalctl -u qasawatch -f
sudo systemctl stop qasawatch
```

## Manual review and safe output testing

`POST /api/manual` accepts one Qasa detail URL, records its review, and sends no output. Promotion and retry require an explicit channel list. Safe mode blocks watcher delivery, production promotion/retry, grouped-batch resend, and test email. After safe mode is disabled, an explicit promotion/retry can deliberately override the safe-scan tombstone for only the selected channels.

For a no-production-output E2E check, set `enabled: false`, `safe_mode: true`, and leave `maps_api_secret_ref` and `scb.data_path` empty if no enrichment calls are wanted. Use `/api/manual` or `/api/run-now`, review the dashboard/database, then configure controlled outputs before turning safe mode off.

## Repository hygiene

[`.gitignore`](.gitignore) excludes DB/WAL sidecars, Chrome profiles, state, outputs, backups, local configuration, and credentials. Keep database and profile paths outside the repository even when ignored.
