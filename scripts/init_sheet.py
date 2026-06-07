"""Create the target Google Sheet tab and write header row.

Usage: python scripts/init_sheet.py

Reads SHEET_ID and SHEET_TAB_NAME from .env. If the sheet id is empty, creates
a brand new spreadsheet owned by the authorized user.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.errors import HttpError

from src.config import configure_logging, load_config
from src.sheets_client import COLUMNS, SheetsClient


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)

    sheet_id = cfg.sheet_id
    if not sheet_id:
        print(
            "SHEET_ID is empty in .env. To create a new spreadsheet, run:\n"
            "  python scripts/create_sheet.py\n"
            "Then set SHEET_ID in .env to the new spreadsheet id.",
            file=sys.stderr,
        )
        return 1

    sheets = SheetsClient(
        sheet_id=sheet_id,
        tab_name=cfg.sheet_tab_name,
        credentials_path=cfg.sheets_credentials_path,
        token_path=cfg.sheets_token_path,
    )
    try:
        sheets.ensure_tab()
    except HttpError as exc:
        print(f"Sheets error: {exc}", file=sys.stderr)
        return 2

    print(f"Sheet ready: {cfg.sheet_tab_name}")
    print("Headers: " + " | ".join(COLUMNS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
