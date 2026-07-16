# QasaWatch

QasaWatch checks a saved Qasa search for new homes. You choose the search and
filters in a local dashboard, and QasaWatch can check it on a schedule.

It can also send matching homes by email or Discord, or save them to Google
Sheets. These connections are optional—you can use QasaWatch locally without
setting up any of them. QasaWatch is an unofficial independent project and is
not affiliated with or endorsed by Qasa.

## Please read before using

Use QasaWatch at your own risk. The author takes no responsibility for account
restrictions, missed or incorrect listings, unwanted notifications, costs from
connected services, or any other outcome from using this project.

Qasa can be strict about automated access. QasaWatch is consciously designed
with that in mind: it checks one saved search at a time, follows only a bounded
number of result pages, avoids reopening known listings, supports timing
variation between checks, and stops for login pages, CAPTCHAs, or incomplete
results instead of trying to bypass them. These precautions reduce unnecessary activity, but they
cannot guarantee that Qasa will permit or ignore its use.

To the author's knowledge, no user has reported an account or access problem
caused by QasaWatch. That is only past experience, not a promise about future
use. Use a sensible checking interval, monitor the app, and stop using it if
Qasa objects or its rules do not allow what you are doing.

## Jump to

- [Local setup](#the-easiest-setup-run-it-on-your-own-computer)
- [First-time setup](#first-time-setup-in-qasawatch)
- [Email](#email)
- [Discord](#discord)
- [Google Sheets](#google-sheets)
- [Google Maps commute times](#google-maps-commute-times)
- [Local SCB area data](#local-scb-area-data-advanced)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## The easiest setup: run it on your own computer

QasaWatch is designed to work locally with Google Chrome. You do not need a
server or a separate database.

### What you need

- [Python 3.12 or newer](https://www.python.org/downloads/)
- [Google Chrome](https://www.google.com/chrome/)
- This project downloaded and extracted to a folder on your computer

If you downloaded a ZIP file, extract it first, open the extracted folder, and
choose **Open in Terminal** or **Open PowerShell here**.

The instructions below use PowerShell on Windows. macOS and Linux commands are
included afterward.

### Windows

Open PowerShell in the project folder, then run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[browser]"
qasawatch
```

The first installation may take a minute. When QasaWatch starts:

1. A separate Chrome window opens for QasaWatch.
2. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your usual browser.
3. Leave the PowerShell window open while QasaWatch is running.

If PowerShell does not allow the activation command, run this once in the same
window and try again:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### macOS or Linux

Open a terminal in the project folder, then run:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[browser]'
qasawatch
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

On Linux, run QasaWatch from a graphical desktop session. A server without a
desktop needs the private Xvfb setup described in the
[operations guide](docs/OPERATIONS.md). QasaWatch recognizes the normal
`google-chrome`, `google-chrome-stable`, `chromium`, and `chromium-browser`
commands, including launchers that hand off to a differently named Chrome
process.

## First-time setup in QasaWatch

Keep **Preview mode** turned on while setting things up. In preview mode you can
test the search without saving listings or sending notifications.

1. In the Chrome window opened by QasaWatch, stay logged out of Qasa if the
   search works without an account. Logging in connects the automated activity
   more directly to your account, so only sign in when it is genuinely
   necessary.
2. On Qasa, create the search you want and choose your filters. If Qasa requires
   a login for this, sign in manually in the QasaWatch Chrome window.
3. Copy the full address of the Qasa search-results page.
4. Paste it into **Qasa search results link** in the QasaWatch dashboard.
5. Add one or more commute destinations if you want travel-time calculations.
   Otherwise, leave this section empty.
6. Choose any additional home, area, or listing preferences you care about.
7. Select **Save settings**.
8. Select **Check now** and review the result.

You can leave notification options switched off. Commute calculations are also
optional; they require a Google Maps API key, but ordinary Qasa searching does
not.

Once the preview result looks correct, turn off **Preview mode** so QasaWatch
can remember which listings it has already seen. Turn on **Automatic
monitoring** if you want checks to run on a schedule, then save the settings
again.

When automatic monitoring is on, QasaWatch checks the saved search at the
interval shown in the dashboard. Keep the app running for scheduled checks to
happen.

### Checking one listing manually

Use **Check one listing** to inspect any individual Qasa listing without adding
it to normal watcher history or sending anything automatically. After reviewing
the result, you can explicitly send it to Discord or email, or save it to
Google Sheets. Each click is treated as an intentional manual send and will
send again even if that listing was sent previously. Automatic watcher checks
still prevent duplicate notifications.

## The database, in plain language

QasaWatch uses a small local database file named `qasawatch.db`. It is created
automatically in the project folder the first time the app starts.

There is nothing else to install or configure:

- No database server
- No database account or password
- No manual database creation

The file stores your settings, previous checks, and which listings have already
been seen. QasaWatch also keeps its dedicated Chrome profile in the
`.qasawatch` folder beside the database.

Do not delete `qasawatch.db` or `.qasawatch` unless you intentionally want to
reset QasaWatch. The Chrome profile may contain your Qasa login, so do not share
or commit it.

If you prefer to keep the database somewhere else, start the app with:

```powershell
qasawatch --database "C:\path\to\qasawatch.db"
```

This is optional. The default local database is suitable for normal use.

## Starting QasaWatch again

After the first installation, open a terminal in the project folder and run:

Windows:

```powershell
.\.venv\Scripts\Activate.ps1
qasawatch
```

macOS or Linux:

```sh
. .venv/bin/activate
qasawatch
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) if the dashboard is not
already open.

Press `Ctrl+C` in the terminal to stop QasaWatch. Its dedicated Chrome window
may remain open. If you had to log in, this preserves the session for the next
start. Otherwise, remaining logged out is recommended.

## Optional notifications and extra features

None of the features below are required. Set up only the ones you want.

Passwords, webhook addresses, and API keys go in a file named `.env` in the
project folder. QasaWatch reads this file when it starts. If you change `.env`,
stop and restart QasaWatch.

Do not share `.env` or commit it to Git. The supplied `.gitignore` already
excludes it.

QasaWatch reads private connection values from the running environment first
and uses `.env` for values that are otherwise missing. Restart QasaWatch after
editing `.env`.

### Email

The easiest email setup is Gmail. QasaWatch uses Gmail's normal mail server, but
it needs a Google **App Password** rather than your usual Google password.

1. Turn on 2-Step Verification for the Google account that will send the email.
2. Create an [App Password](https://support.google.com/accounts/answer/185833)
   for QasaWatch.
3. Create or open `.env` in the project folder and add:

   ```text
   QASAWATCH_SMTP_PASSWORD=your-16-character-app-password
   ```

4. Restart QasaWatch.
5. In the dashboard, open **Notifications and saving**.
6. Turn on **Send email notifications**.
7. Enter the people who should receive the messages.
8. Enter the Gmail address as **Sender email** and choose **Gmail** as the
   provider.
9. Save the settings.
10. Temporarily turn off **Preview mode**, then use **Test email**.

Preview mode blocks email, including test messages. You can turn preview mode
back on after the test if you are not ready to begin normal notifications.

For another email provider, choose **Another provider** and open the advanced
email settings. Enter the SMTP server, port, security mode, and username given
by your email provider. Keep the password in `.env`:

```text
QASAWATCH_SMTP_PASSWORD=your-email-password
```

QasaWatch automatically reads the password from
`QASAWATCH_SMTP_PASSWORD`; there is no password or reference field in the
dashboard.

### Discord

Discord notifications use a webhook: a private address that lets QasaWatch post
to one channel.

1. In Discord, open the settings for the server and channel you want to use.
2. Create a webhook and copy its URL. Discord's
   [webhook guide](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
   shows the current steps.
3. Add the URL to `.env`:

   ```text
   QASAWATCH_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```

4. Restart QasaWatch.
5. Turn on **Send Discord notifications** in the dashboard and save.
6. Select **Send test message**.

The dashboard automatically uses `QASAWATCH_DISCORD_WEBHOOK_URL`. The webhook
URL is not displayed or stored in the dashboard. Treat it like a password.

### Google Sheets

QasaWatch can append each matching home to a Google Sheet. This setup uses a
Google service account: a small Google identity created for QasaWatch.

1. Create or select a project in the
   [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the
   [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com).
3. Create a service account and download a JSON key. Google's
   [credential guide](https://developers.google.com/workspace/guides/create-credentials#service-account)
   explains these steps.
4. Put the downloaded JSON file somewhere private. Do not commit it to Git.
5. Copy the service account's email address from the JSON file or Google Cloud.
6. Create the Google Sheet you want to use and share it with that service
   account email as an **Editor**.
7. Make sure the Sheet has a tab named `Listings`, or choose another tab name
   in the QasaWatch dashboard.
8. Add the full path to the JSON key to `.env`. For example:

   ```text
   QASAWATCH_GOOGLE_SERVICE_ACCOUNT_JSON=C:\Users\you\Documents\qasawatch-key.json
   ```

9. Restart QasaWatch.
10. In the dashboard, turn on **Save to Google Sheets** and open
    **Google Sheets connection**.
11. Enter the spreadsheet ID. It is the long value between `/d/` and `/edit`
    in the Sheet's web address:

    ```text
    https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
    ```

12. Enter the worksheet tab name and save the settings.
13. Use **Test Google Sheets**. The test only reads spreadsheet information; it
    does not add a row.

Google Sheets output is blocked while Preview mode is on. When you are ready to
test it, turn Preview mode off and run a controlled check.

### Google Maps commute times

Google Maps is optional for basic monitoring. It is used to geocode addresses
for SCB demographic matching when Qasa does not expose coordinates, and to
calculate travel times when commute destinations are configured. You can use
SCB demographics with zero commute destinations, but should configure the Maps
key so listings without coordinates can still be located. Google may require
billing for these APIs, so review its pricing and set a budget or quota before
enabling them.

1. Create or select a project in the
   [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Geocoding API** and **Routes API**. The
   [Google Maps documentation](https://developers.google.com/maps/documentation)
   links to both products.
3. Create an API key. Restrict the key to those two APIs where possible.
4. Add the key to `.env`:

   ```text
   QASAWATCH_GOOGLE_MAPS_API_KEY=your-api-key
   ```

5. Restart QasaWatch.
6. Add as many commute destinations as you need and choose whether each journey
   should arrive by or leave at 08:00 Europe/Stockholm time.
7. Save and use **Test Google Maps**. The test checks geocoding and also checks
   Routes when at least one commute destination is configured.
8. Keep Preview mode on and select **Check now**.

Only set a maximum commute filter after Maps is working. Without a Maps key,
QasaWatch can still search Qasa. Commutes will be unavailable, and SCB matching
will work only for listings where Qasa already supplied coordinates.

### Local SCB area data (advanced)

This optional feature adds local population data from Statistics Sweden (SCB)
to listings. Separate helpers can build either a smaller Stockholm County file
or a nationwide file covering all 21 counties.

For the smaller Stockholm County dataset:

```powershell
python scripts/build_stockholm_scb.py
```

For the complete Sweden dataset:

```powershell
python scripts/build_sweden_scb.py
```

The nationwide download takes longer and produces a substantially larger file.
Both builders download public SCB DeSO 2025 geography and demographic data.

The commands create one of the following files:

- `data/scb/stockholm-deso-2025.geojson` for Stockholm County
- `data/scb/sweden-deso-2025.geojson` for nationwide coverage

Then open **Location data and map connection** in the dashboard and use:

- **Local data file:** `data/scb/stockholm-deso-2025.geojson`
- **Area ID column:** `deso_id`
- **Area name column:** `deso_name`
- **Data year:** `2025`
- **Coordinate system:** `EPSG:4326`

In **Advanced JSON controls**, set the location-data configuration to:

```json
{
  "data_path": "data/scb/stockholm-deso-2025.geojson",
  "id_column": "deso_id",
  "name_column": "deso_name",
  "demographic_mapping": {
    "population": "population",
    "foreign_background_percent": "foreign_background_percent"
  },
  "vintage": "2025",
  "crs": "EPSG:4326"
}
```

Save and run a check in Preview mode. You can then use the local population
filters in the dashboard. The included file does not contain average age, so
leave the average-age filter empty.

Use `data/scb/stockholm-deso-2025.geojson` for Stockholm County only, or
`data/scb/sweden-deso-2025.geojson` for nationwide coverage. Both use the same
dashboard column settings. Using another custom SCB GeoJSON file requires
matching its column names in the same location-data settings.

For less common settings and technical details, see the
[configuration guide](docs/CONFIGURATION.md).

## Troubleshooting

### Chrome does not open

Make sure Google Chrome is installed in its normal location, then stop and
restart QasaWatch. On Linux, confirm that the terminal has a graphical display:

```sh
echo "$DISPLAY"
echo "$WAYLAND_DISPLAY"
```

If Chrome or Chromium is installed in an unusual location, add its full path to
`.env`, then restart QasaWatch:

```dotenv
QASAWATCH_CHROME_EXECUTABLE=/full/path/to/google-chrome
```

Do not start QasaWatch with `sudo`; run it as your normal desktop user. For a
headless Linux server, use the dedicated non-root service account and Xvfb
configuration from the operations guide.

### Qasa asks you to sign in or complete a CAPTCHA

First check whether the saved search works while logged out. If a login is
actually required, use the separate Chrome window opened by QasaWatch and sign
in manually. Complete any CAPTCHA yourself; QasaWatch does not try to bypass
one. Then return to the dashboard and select **Check now** again.

### The dashboard does not open

Check that the terminal still shows QasaWatch running, then visit
[http://127.0.0.1:8000](http://127.0.0.1:8000) directly.

### The `qasawatch` command is not found

Make sure the virtual environment is active. You can also start it with:

```powershell
python -m qasawatch.cli
```

### I want to start over

Stop QasaWatch first. Renaming or removing `qasawatch.db` resets saved settings
and history. Renaming or removing `.qasawatch` resets the dedicated Chrome
profile. If your search genuinely requires an account, you may need to sign in
again afterward.

## License

QasaWatch is available under the [MIT License](LICENSE).

It is provided as-is, without warranty. See the license and the
[use-at-your-own-risk notice](#please-read-before-using) above.

## For development or server deployment

These steps are not needed for ordinary local use.

Run the test suite with:

```sh
python -m pip install -e '.[browser,test]'
pytest
```

The repository also supports `uv`:

```sh
uv sync --all-extras
uv run pytest
uv run qasawatch
```

For a continuously running Linux service, backups, remote browser access, and
security guidance, see the [operations guide](docs/OPERATIONS.md). Example
systemd files are available in [`deployment/systemd`](deployment/systemd/).
