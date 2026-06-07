"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return value or ""


def _get_path(name: str, default: str) -> Path:
    raw = _get(name, default)
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Env var {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    openai_model: str

    gmail_credentials_path: Path
    gmail_token_path: Path
    sheets_credentials_path: Path
    sheets_token_path: Path
    sheet_id: str
    sheet_tab_name: str

    state_db_path: Path
    lookback_hours: int
    gmail_max_results: int
    log_level: str
    dry_run: bool


def load_config() -> Config:
    return Config(
        openai_api_key=_get("OPENAI_API_KEY", required=True),
        openai_model=_get("OPENAI_MODEL", "gpt-4o-mini"),
        gmail_credentials_path=_get_path("GMAIL_CREDENTIALS_PATH", "./credentials/gmail_credentials.json"),
        gmail_token_path=_get_path("GMAIL_TOKEN_PATH", "./credentials/gmail_token.json"),
        sheets_credentials_path=_get_path("SHEETS_CREDENTIALS_PATH", "./credentials/sheets_credentials.json"),
        sheets_token_path=_get_path("SHEETS_TOKEN_PATH", "./credentials/sheets_token.json"),
        sheet_id=_get("SHEET_ID", required=True),
        sheet_tab_name=_get("SHEET_TAB_NAME", "Applications"),
        state_db_path=_get_path("STATE_DB_PATH", "./data/state.db"),
        lookback_hours=_get_int("LOOKBACK_HOURS", 4),
        gmail_max_results=_get_int("GMAIL_MAX_RESULTS", 100),
        log_level=_get("LOG_LEVEL", "INFO"),
        dry_run=_get_bool("DRY_RUN", False),
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
