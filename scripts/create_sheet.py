"""Create a brand-new Google Sheet for job tracking and print its id.

Run once, then copy the printed SHEET_ID value into your .env.
"""
from __future__ import annotations

import sys

from googleapiclient.discovery import build

from src.config import configure_logging, load_config
from src.sheets_client import SHEETS_SCOPES, _ensure_credentials


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)
    creds = _ensure_credentials(cfg.sheets_credentials_path, cfg.sheets_token_path)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheet = svc.spreadsheets().create(
        body={
            "properties": {"title": "Job Applications"},
            "sheets": [{"properties": {"title": cfg.sheet_tab_name}}],
        },
        fields="spreadsheetId,spreadsheetUrl",
    ).execute()
    print("Created spreadsheet")
    print("  SHEET_ID:", sheet.get("spreadsheetId"))
    print("  URL:     ", sheet.get("spreadsheetUrl"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
