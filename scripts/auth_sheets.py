"""One-time OAuth flow for Google Sheets. Run locally (it opens a browser)."""
from __future__ import annotations

import sys

from src.config import configure_logging, load_config
from src.sheets_client import SheetsClient


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)
    if not cfg.sheets_credentials_path.exists():
        print(
            f"Missing Sheets credentials at {cfg.sheets_credentials_path}.\n"
            "Create a Desktop OAuth client in Google Cloud Console and save the JSON there.",
            file=sys.stderr,
        )
        return 1
    SheetsClient(
        sheet_id=cfg.sheet_id,
        tab_name=cfg.sheet_tab_name,
        credentials_path=cfg.sheets_credentials_path,
        token_path=cfg.sheets_token_path,
    )
    print(f"Sheets auth successful. Token saved to {cfg.sheets_token_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
