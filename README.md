# Mailman

A Python cron job that watches your Gmail every 3 hours, uses OpenAI to figure
out which messages are about job applications, and logs them into a Google
Sheet — appending new applications, updating existing ones when you get
rejected/shortlisted/offered.

```
Gmail  ─►  OpenAI  ─►  Google Sheets
              ▲
              │  (extract: company, role, status, platform)
              │
       state.db  (last_run, processed email ids)
```

## What it does

1. Every 3 hours, fetches emails received since the last successful run
   (Gmail `after:` query — no manual API quota math).
2. Skips messages it's already processed (idempotent — safe to run as often
   as you like).
3. Sends each new email's subject + body to OpenAI with a strict JSON
   schema. The model returns:
   - `is_job_related` / `action` (`new_application` | `status_update` | `irrelevant`)
   - `company`, `role`, `platform` (LinkedIn / Greenhouse / Lever / …)
   - `status` (Applied / Shortlisted / Interview / Assessment / Offer / Rejected / Withdrawn / Unknown)
   - `confidence`, `reasoning`
4. In Google Sheets:
   - If a row with the same `Company + Role` exists → update its Status,
     Last Update, Notes, and Email Subject (preserving the original Date
     Applied).
   - Otherwise → append a new row.
5. Updates the SQLite state DB with the new `last_run` cursor and a record
   of every processed message id.

## Project layout

```
mailman/
├── src/
│   ├── config.py          # env loader, paths
│   ├── models.py          # Pydantic schemas
│   ├── state.py           # SQLite (last_run + processed_emails)
│   ├── gmail_client.py    # OAuth + Gmail search/fetch
│   ├── sheets_client.py   # OAuth + Sheets read/append/update
│   ├── llm.py             # OpenAI structured-output extractor
│   └── main.py            # orchestrator (cron entrypoint)
├── scripts/
│   ├── auth_gmail.py      # one-time OAuth for Gmail
│   ├── auth_sheets.py     # one-time OAuth for Sheets
│   ├── create_sheet.py    # create a new spreadsheet, print its id
│   └── init_sheet.py      # create the tab + header row
├── launchd/
│   └── com.user.mailman.plist   # macOS launchd schedule (every 3h)
├── crontab.example        # Linux/BSD crontab line
├── requirements.txt
├── .env.example
└── README.md
```

## 1. Google Cloud setup (one-time)

You need **two OAuth 2.0 Client IDs** of type *Desktop app* in the same
Google Cloud project, or two separate projects if you prefer. Both can use
the same `credentials.json` if you enable both APIs on the same project —
just download a new Desktop-client JSON after enabling the second API.

1. Go to https://console.cloud.google.com/
2. Create or pick a project.
3. **APIs & Services → Library** → enable:
   - **Gmail API**
   - **Google Sheets API**
4. **APIs & Services → OAuth consent screen** → configure:
   - User type: *External* (or *Internal* for Workspace)
   - Add scopes: `gmail.readonly`, `spreadsheets`
   - Add yourself as a test user (while the app is in "Testing" status)
5. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   → Application type: *Desktop app*. Download the JSON.
6. Save it to `credentials/gmail_credentials.json` (the same file works for
   Sheets if both APIs are enabled in the same project; otherwise download a
   second one and save it as `credentials/sheets_credentials.json`).

## 2. Install

```bash
cd /Users/abhishekvishwakarma/Ideas/mailman
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

```dotenv
OPENAI_API_KEY=sk-...
SHEET_ID=                     # leave blank if you'll run create_sheet.py
SHEET_TAB_NAME=Applications
LOG_LEVEL=INFO
DRY_RUN=false                  # set true for a safe first run
```

## 3. Authenticate (one-time, runs a local browser flow)

```bash
python scripts/auth_gmail.py
python scripts/auth_sheets.py
```

## 4. Create / initialize the sheet

```bash
python scripts/create_sheet.py   # prints a new SHEET_ID, copy it into .env
python scripts/init_sheet.py     # creates the "Applications" tab + header
```

## 5. Test run

```bash
DRY_RUN=true python -m src.main
```

You should see logs like:

```
Mailman run starting (dry_run=True)
Looking at emails since 2026-06-07T00:00:00+00:00
Gmail search query: 'after:2026/06/07 '
Found 4 candidate messages
Classifying msg=18d2... subject='Your application to Acme was received'
[DRY-RUN] would_append row=None for Senior Backend Engineer @ Acme
Run summary: new=1 updated=0 skipped=3 total_seen=4
```

When you trust it, drop `DRY_RUN=true` (or set `DRY_RUN=false` in `.env`).

## 6. Schedule it (every 3 hours)

### macOS — launchd (recommended)

```bash
# 1. edit launchd/com.user.mailman.plist:
#    - replace /path/to/your/venv/bin/python
#    - confirm WorkingDirectory
mkdir -p logs
cp launchd/com.user.mailman.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.user.mailman.plist

# verify
launchctl list | grep mailman

# tail logs
tail -f logs/launchd.out.log
```

### Linux / BSD — cron

```bash
crontab -e
# append:
7 */3 * * * cd /Users/abhishekvishwakarma/Ideas/mailman && .venv/bin/python -m src.main >> logs/cron.log 2>&1
```

(The minute `7` is just to desynchronize from other jobs. Any minute works.)

## Configuration reference

| Env var | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | _required_ | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Any chat model that supports `json_schema` response_format |
| `GMAIL_CREDENTIALS_PATH` | `./credentials/gmail_credentials.json` | OAuth client JSON |
| `GMAIL_TOKEN_PATH` | `./credentials/gmail_token.json` | Refreshed automatically |
| `SHEETS_CREDENTIALS_PATH` | `./credentials/sheets_credentials.json` | OAuth client JSON |
| `SHEETS_TOKEN_PATH` | `./credentials/sheets_token.json` | Refreshed automatically |
| `SHEET_ID` | _required_ | Spreadsheet id (the long string in the URL) |
| `SHEET_TAB_NAME` | `Applications` | Tab to read/write |
| `STATE_DB_PATH` | `./data/state.db` | SQLite state file |
| `LOOKBACK_HOURS` | `4` | Safety floor for the first run / large gaps |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DRY_RUN` | `false` | If `true`, never write to Sheets |

## How the matching works

A new email is appended only if no existing row has the same
`Company + Role` (case-insensitive, whitespace-normalized). When a match is
found, the original `Date Applied` is preserved, and the script picks the
"more informative" status — so a Rejected email arriving after an Applied
row will turn the row into Rejected, not regress it back to Applied.

## Troubleshooting

- **"Missing Gmail credentials"** — the OAuth JSON path is wrong; check
  `GMAIL_CREDENTIALS_PATH` in `.env`.
- **"The application does not exist"** on Sheets — your `SHEET_ID` is wrong
  or the service-account / OAuth user doesn't have access. Re-share the
  sheet with the same Google account you authenticated with.
- **All messages are classified as `irrelevant`** — set `LOG_LEVEL=DEBUG`
  and look at the model's `reasoning`; you may need to tweak the prompt in
  `src/llm.py`.
- **Rate limits** — Gmail is ~250 quota units / user / second. The script
  caps at 100 messages per run; raise `max_results` in `main.py:run()` if
  you expect bursts.

## Privacy

The email body (truncated to 6 KB) is sent to OpenAI for classification.
No data is sent to any other service. The Google Sheet lives in your own
Google Drive. The OAuth tokens are stored locally in `credentials/`.
