"""One-time OAuth flow for Gmail. Run locally (it opens a browser)."""
from __future__ import annotations

import sys

from src.config import PROJECT_ROOT, configure_logging, load_config
from src.gmail_client import GmailClient


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)
    creds = cfg.gmail_credentials_path
    token = cfg.gmail_token_path
    if not creds.exists():
        print(
            f"Missing Gmail credentials at {creds}.\n"
            "Create a Desktop OAuth client in Google Cloud Console and save the JSON there.",
            file=sys.stderr,
        )
        return 1
    GmailClient(credentials_path=creds, token_path=token)
    print(f"Gmail auth successful. Token saved to {token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
