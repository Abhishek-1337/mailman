"""Google Sheets wrapper: header management, row lookup, append/update."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import JobApplication, JobStatus

log = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# (Header text, column index 1-based). Keep order = write order.
COLUMNS: Tuple[str, ...] = (
    "Date Applied",
    "Company",
    "Role",
    "Platform",
    "Status",
    "Last Update",
    "Notes",
    "Email Subject",
)
COL_INDEX = {name: i + 1 for i, name in enumerate(COLUMNS)}


def _ensure_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SHEETS_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing Sheets access token")
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Sheets credentials.json not found at {credentials_path}."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SHEETS_SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        log.info("Saved Sheets token to %s", token_path)
    return creds


class SheetsClient:
    def __init__(self, sheet_id: str, tab_name: str, credentials_path: Path, token_path: Path) -> None:
        creds = _ensure_credentials(credentials_path, token_path)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sheet_id = sheet_id
        self.tab_name = tab_name
        self._range_base = f"'{tab_name}'"

    def _range(self, a1: str) -> str:
        return f"{self._range_base}!{a1}"

    def ensure_tab(self) -> None:
        """Create the tab and header row if missing. Idempotent."""
        try:
            meta = self._service.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        except HttpError as exc:
            log.error("Sheets metadata fetch failed: %s", exc)
            raise
        existing_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if self.tab_name not in existing_tabs:
            log.info("Creating tab %r", self.tab_name)
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": self.tab_name}}}]},
            ).execute()

        header_range = self._range("A1:H1")
        header_values = self._service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=header_range
        ).execute().get("values", [])
        if not header_values or header_values[0] != list(COLUMNS):
            log.info("Writing header row: %s", COLUMNS)
            self._service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=header_range,
                valueInputOption="RAW",
                body={"values": [list(COLUMNS)]},
            ).execute()

    def list_rows(self) -> List[List[str]]:
        result = self._service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=self._range("A1:H"),
        ).execute()
        rows = result.get("values", []) or []
        # Pad short rows to match COLUMNS length so indexing is safe.
        width = len(COLUMNS)
        return [r + [""] * (width - len(r)) for r in rows]

    def find_row(self, company: str, role: str) -> Optional[int]:
        """Return 1-based row number (in the sheet, including header) of a matching
        company+role, or None. Matching is case-insensitive and whitespace-normalized."""
        rows = self.list_rows()
        c_target = _norm(company)
        r_target = _norm(role)
        for idx, row in enumerate(rows[1:], start=2):  # skip header
            if len(row) < 3:
                continue
            if _norm(row[COL_INDEX["Company"] - 1]) == c_target and _norm(
                row[COL_INDEX["Role"] - 1]
            ) == r_target:
                return idx
        return None

    def append(self, app: JobApplication) -> None:
        values = [_row_from_app(app)]
        log.info("Appending row for %s @ %s", app.role, app.company)
        self._service.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=self._range("A1"),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

    def update(self, row_number: int, app: JobApplication) -> None:
        values = [_row_from_app(app)]
        rng = self._range(f"A{row_number}:H{row_number}")
        log.info("Updating row %d for %s @ %s", row_number, app.role, app.company)
        self._service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=rng,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()


def _row_from_app(app: JobApplication) -> List[str]:
    return [
        app.date_applied,
        app.company,
        app.role,
        app.platform,
        app.status.value if isinstance(app.status, JobStatus) else str(app.status),
        app.last_update,
        app.notes,
        app.email_subject,
    ]


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def app_from_parts(
    *,
    date_applied: str,
    company: str,
    role: str,
    platform: str,
    status: JobStatus,
    last_update: str,
    notes: str,
    email_subject: str,
) -> JobApplication:
    return JobApplication(
        date_applied=date_applied,
        company=company,
        role=role,
        platform=platform,
        status=status,
        last_update=last_update,
        notes=notes,
        email_subject=email_subject,
    )


def row_to_app(row: Sequence[str]) -> JobApplication:
    def cell(name: str) -> str:
        return row[COL_INDEX[name] - 1] if len(row) >= COL_INDEX[name] else ""
    raw_status = cell("Status")
    try:
        status = JobStatus(raw_status)
    except ValueError:
        status = JobStatus.UNKNOWN
    return JobApplication(
        date_applied=cell("Date Applied"),
        company=cell("Company"),
        role=cell("Role"),
        platform=cell("Platform"),
        status=status,
        last_update=cell("Last Update"),
        notes=cell("Notes"),
        email_subject=cell("Email Subject"),
    )
