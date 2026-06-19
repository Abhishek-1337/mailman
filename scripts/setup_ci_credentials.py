#!/usr/bin/env python3
"""Write credential JSON files from GitHub Actions secrets (base64 or raw JSON)."""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def decode_secret(name: str) -> str:
    raw = os.environ.get(name, "")
    if not raw.strip():
        raise SystemExit(f"Missing or empty secret: {name}")

    trimmed = raw.strip()
    if trimmed.startswith("{"):
        text = trimmed
    else:
        cleaned = "".join(trimmed.split())
        try:
            text = base64.b64decode(cleaned, validate=True).decode("utf-8")
        except Exception as exc:
            raise SystemExit(
                f"Failed to base64-decode {name}: {exc}\n"
                f"Re-encode with: python scripts/encode_for_github.py credentials/<file>.json"
            ) from exc

    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Secret {name} is not valid JSON after decode: {exc}") from exc

    return text


def write_secret(name: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(decode_secret(name), encoding="utf-8")
    print(f"Wrote {path}")


def main() -> None:
    creds_dir = Path("credentials")
    write_secret("GMAIL_CREDENTIALS_JSON", creds_dir / "gmail_credentials.json")
    write_secret("GMAIL_TOKEN_JSON", creds_dir / "gmail_token.json")
    write_secret("SHEETS_TOKEN_JSON", creds_dir / "sheets_token.json")

    sheets_creds = os.environ.get("SHEETS_CREDENTIALS_JSON", "").strip()
    sheets_path = creds_dir / "sheets_credentials.json"
    if sheets_creds:
        write_secret("SHEETS_CREDENTIALS_JSON", sheets_path)
    else:
        sheets_path.write_text(
            (creds_dir / "gmail_credentials.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        print(f"Wrote {sheets_path} (copied from gmail_credentials.json)")


if __name__ == "__main__":
    main()
