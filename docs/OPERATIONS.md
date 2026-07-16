# Operations runbook

The packaged `qasawatch` command initializes the database, calls
`recover_interrupted_work()`, creates persistent Chrome/browser state, wires
configured Google Maps, SCB, Google Sheets, Discord, and SMTP providers, and
starts/adopts the persistent Chrome process before starting the FastAPI
scheduler. This occurs even when watching is disabled so the dedicated profile
is immediately available for manual login. Do not expose the dashboard or CDP
port directly to the Internet.

## Host layout and permissions

Recommended Linux paths:

```text
/opt/qasawatch/                 checked-out release and virtual environment
/var/lib/qasawatch/qasawatch.db persistent SQLite database
/var/lib/qasawatch/.qasawatch/chrome-profile/ dedicated authenticated Chrome profile
/var/lib/qasawatch/scb/          optional SCB GeoJSON
/var/backups/qasawatch/          protected, encrypted backups
/etc/qasawatch/qasawatch.env     root/qasawatch-readable secret environment file
```

Run as a dedicated `qasawatch` account. Make `/var/lib/qasawatch` and its
profile `0700`; make the environment file `0600`. The Chrome profile and all
database backups can contain credentials/personal data, so encrypt backups and
limit restoration access.

Install [qasawatch.env.example](../deployment/systemd/qasawatch.env.example) as
`/etc/qasawatch/qasawatch.env` and substitute protected secret values. Install
[qasawatch.service.example](../deployment/systemd/qasawatch.service.example) as
the basis for `/etc/systemd/system/qasawatch.service`. Its `ExecStart` uses the
packaged command; keep `safe_mode` enabled until provider credentials and
destinations have been tested.

## Service lifecycle

After installing or changing a unit:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now qasawatch
sudo systemctl status qasawatch
sudo journalctl -u qasawatch -f
sudo systemctl restart qasawatch
sudo systemctl stop qasawatch
```

Use `systemctl status` and the dashboard `/api/status` together: systemd only
tells you whether the process is alive; the dashboard shows browser readiness,
the persisted next scan, runs, delivery states, and recent errors. The scheduler
uses a durable scan lease, so a second instance reports overlap rather than
scanning concurrently. Long scans renew that lease and expose lease health in
the dashboard. Run only one supervisor per database/profile.

Bind Uvicorn/reverse proxy to `127.0.0.1` or a private interface and require
authentication at the proxy/VPN boundary. The shipped dashboard has no built-in
authentication. Protect the API as carefully as the profile: it can configure
watching and invoke manual processing.

## First production activation

1. Provision the protected paths and Chrome, install dependencies, and start a
   graphical local session for the dedicated profile.
2. Log into Qasa manually and set the desired filters in that profile. Start
   from the supplied exact filtered URL in `config.example.json`, then copy a
   newly chosen URL if filters change.
3. Save configuration from [config.example.json](../config.example.json) with
   real (or controlled test) values. Commute destinations and Google Maps are
   optional; an enabled watcher can run without either.
4. Keep `enabled: false` and `safe_mode: true`; use a manual listing inspection
   and a scan to verify parsing and browser readiness. No output should be sent.
   The 2026-07-14 live verification of the supplied URL, using a persistent
   profile, found 109 canonical candidates with no outputs or enrichment API
   requests; treat that as an observation, not an expected fixed count.
5. Verify Sheets/Discord/SMTP access individually with controlled destinations.
   Keep email test disabled by safe mode until the recipient is confirmed.
6. Turn off safe mode only after reviewing URLs, recipients, Sheet sharing,
   webhook, notifications, and Maps/SCB settings. Then set `enabled: true`.

## Browser access on a headless Linux VM

Use a real Chrome window on a private X display for first login and for manual
authentication/CAPTCHA recovery. A common stack is:

```text
Xvfb (:99, no TCP listener) -> lightweight window manager (for example Openbox)
  -> Chrome with its dedicated user-data-dir
  -> x11vnc bound to 127.0.0.1
  -> noVNC/websockify bound to 127.0.0.1 (optional)
```

The deployment folder includes separate Xvfb, Openbox, x11vnc, and noVNC unit
examples. Start/enable the Openbox unit only for an interactive desktop, and
the VNC/noVNC units only when remote browser access is required. The main unit
uses `DISPLAY=:99` and depends on the Xvfb unit; remove those two lines on a
host using a different graphical arrangement. The graphical units intentionally
share `/tmp/.X11-unix` (`PrivateTmp=false`) so Chrome can reach Xvfb; compensate
by keeping the dedicated service account and all remote bridges private. Bind x11vnc/noVNC to loopback,
set a strong VNC password, and do not rely on VNC as encryption. Access it
through one of these controls:

- SSH tunnel, for example `ssh -L 6080:127.0.0.1:6080 operator@vm`, then open
  the local noVNC URL;
- a private VPN; or
- a TLS-terminating authenticated reverse proxy on a private network.

Never bind CDP (`--remote-debugging-address=127.0.0.1`), VNC, or noVNC to a
public interface. CDP provides browser control and cookie access. When a CAPTCHA
or login page is detected, the scan is quarantined; open the protected console,
complete the challenge yourself, and run a safe verification scan afterward.

## Page state and incident response

| Dashboard/browser state | Meaning | Operator action |
| --- | --- | --- |
| `ready` | Stable listing content. | Normal processing may continue. |
| `empty` | A stable explicit empty-result marker. | Normal empty scan; do not infer emptiness from a blank/loading page. |
| `incomplete` | Loading/unstable/insufficient evidence. | No output should be trusted; retry later and inspect browser if persistent. |
| `auth_required` | Qasa login/session expired. | Use protected profile console to log in again. |
| `captcha` | Bot challenge detected. | Solve manually; do not add CAPTCHA-bypass automation. |
| `error` | Page/parser/browser error. | Check logs/network; run a safe scan after repair. |

A browser crash/restart is normally recovered by the Chrome host on the next
connection. If it repeats, stop the app, confirm no second supervisor holds the
profile, preserve logs, and inspect free disk/RAM. Do not delete the profile to
“fix” a login issue; that destroys the session. If you deliberately reset it,
take an encrypted backup first and expect to log in again.

An incomplete page raises a quarantine error and finishes the run as failed;
it is not treated as a successful zero-result scan. This protects against
accidental “no new listings” notifications and delivery decisions based on a
half-rendered page.

## Manual processing, promotion, and retries

Manual processing is review, not a covert watcher run:

```text
POST /api/manual                 detail page -> review history + result, no outputs
POST /api/manual/promote         review ID + explicit channels -> selected outputs
POST /api/retry                  listing ID + explicit channels -> resend failures
POST /api/run-now                results-page watcher scan (including when disabled)
```

Manual requests accept Qasa listing URLs containing `/home/`, not search URLs.
Promotion and retry with an empty channel list do not produce outputs. In safe
mode, any requested production channel and test email are refused. This makes a
safe manual E2E protocol possible: leave safe mode on, inspect a known listing,
review the stored JSON/filter decision and dashboard, then only turn safe mode
off for a deliberately selected output test.

An explicit manual email promotion is always one message for that reviewed
listing, even when watcher email is configured as one grouped message per scan.
Safe watcher scans are transient dry runs: they report parsing, enrichment and
filter outcomes in the run statistics but create no watcher listing,
deduplication, or delivery records. A listing first encountered during a safe
scan is consequently still new when it is later encountered in production.

On an upgraded database, legacy safe-mode delivery tombstones remain skipped.
They are not automatically backfilled because doing so could unexpectedly send
many old notifications. Review individual listings and use explicit retry or
promotion when an old delivery should be sent.

Each delivery record has durable state and a stable idempotency key. Application
startup calls `recover_interrupted_work()`. Interrupted Sheets deliveries return
to pending because the stable row key can be checked remotely. Interrupted
Discord and SMTP sends enter manual review because their remote outcome may be
unknowable; confirm delivery in the destination before marking them delivered or
retryable. Do not hand-edit delivery state.

## Backups and restore

SQLite runs in WAL mode. Do **not** back up the main `.db` with plain `cp` while
the service is active: it may omit committed WAL data. Preferred online backup:

```sh
sudo install -d -m 0700 -o qasawatch -g qasawatch /var/backups/qasawatch
sudo -u qasawatch sqlite3 /var/lib/qasawatch/qasawatch.db \
  ".backup '/var/backups/qasawatch/qasawatch-$(date +%F).db'"
sudo -u qasawatch sqlite3 /var/backups/qasawatch/qasawatch-YYYY-MM-DD.db 'PRAGMA integrity_check;'
```

The SQLite `.backup` operation creates a consistent snapshot across WAL state.
Test recovery in an isolated directory before relying on a backup. Back up the
environment file separately with encryption/access control. Chrome profile
backup is optional and unusually sensitive; only copy/archive it after Chrome
is fully stopped, never while it is running, and protect it at least as tightly
as credentials. Prefer a documented re-login procedure over routine profile
copies.

Restore database state:

1. Stop QasaWatch and ensure no other process uses the database.
2. Move—not delete—the current DB and its `-wal`/`-shm` sidecars to an incident
   directory.
3. Copy the tested backup to the configured database path; do not restore stale
   `-wal`/`-shm` sidecars with it.
4. Set ownership/mode, start the service, inspect logs and `/api/status`, then
   use safe mode for the first scan.

## Updates and rollback

1. Take a verified SQLite backup and capture the deployed revision/config
   checksum. Preserve the database and profile outside the release directory.
2. Stop the service, install/update the release into `/opt/qasawatch`, and run
   its focused test suite: `uv run pytest` (or activated-venv `pytest`).
3. Review schema compatibility. The code refuses a database schema newer than
   it understands; do not downgrade after a migration without a restoration
   plan.
4. Start the service in safe mode, check browser login/readiness, perform a
   manual inspection, then re-enable outputs/scheduling deliberately.
5. To roll back, stop service, restore the previous release/venv, and restore a
   compatible verified database backup if the newer release changed schema.

## Troubleshooting quick checks

```sh
sudo systemctl status qasawatch
sudo journalctl -u qasawatch -n 200 --no-pager
sudo -u qasawatch test -r /etc/qasawatch/qasawatch.env
sudo -u qasawatch test -w /var/lib/qasawatch
sudo -u qasawatch sqlite3 /var/lib/qasawatch/qasawatch.db 'PRAGMA integrity_check;'
```

For a failed Google call, first verify API enablement, server-key API/IP
restriction, quota, and the secret reference/environment variable—not just the
network. For Sheets, verify the exact spreadsheet is shared to the service
account. For SMTP, verify the selected TLS mode/port and that both username and
password are present together. For SCB failures, check the GeoJSON metadata,
field mapping, source/vintage expectation, and WGS84 CRS; absence is a partial
enrichment condition, malformed present data is a configuration error.
