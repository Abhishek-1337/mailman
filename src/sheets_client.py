"""Google Sheets wrapper: header management, row lookup, append/update."""
from __future__ import annotations

import logging
import os
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

# Status -> (background hex, text hex) used for conditional formatting of the
# Status column (E). Green = Offer, red = Rejected, others aid scanning.
STATUS_COLORS = {
    JobStatus.APPLIED: ("#F1F3F4", "#5F6368"),
    JobStatus.SHORTLISTED: ("#FEF7E0", "#B06000"),
    JobStatus.ASSESSMENT: ("#FEEFC3", "#E8710A"),
    JobStatus.INTERVIEW: ("#E8F0FE", "#1967D2"),
    JobStatus.OFFER: ("#E6F4EA", "#137333"),
    JobStatus.REJECTED: ("#FCE8E6", "#A50E0E"),
    JobStatus.WITHDRAWN: ("#F1F3F4", "#80868B"),
}

# Values that must never trigger a company match on their own: every email
# the LLM could not attribute would otherwise collapse into a single row.
_UNMATCHABLE_COMPANIES = frozenset({"", "unknown"})


def _ensure_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds: Optional[Credentials] = None
    if token_path.exists() and token_path.stat().st_size > 0:
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SHEETS_SCOPES)
        except Exception as exc:
            log.warning("Sheets token file is corrupt or invalid (%s); treating as missing.", exc)
            creds = None
    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing Sheets access token")
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds or not creds.valid:
        if os.getenv("CI"):
            raise RuntimeError(
                f"Sheets token at {token_path} is invalid or missing and could not be refreshed. "
                "Run scripts/auth_sheets.py locally to generate a fresh token, "
                "then base64-encode the file and update the SHEETS_TOKEN_JSON secret."
            )
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

        self.ensure_status_formatting()

    def ensure_status_formatting(self) -> None:
        """Color the Status column (E) by value via conditional formatting.

        Green for Offer, red for Rejected, plus distinct colors for the other
        statuses (see STATUS_COLORS). Idempotent: only rules not already
        present are added. Best-effort — a failure here logs a warning and
        never breaks a mail run.
        """
        try:
            meta = self._service.spreadsheets().get(
                spreadsheetId=self.sheet_id,
                fields="sheets(properties(sheetId,title),conditionalFormats)",
            ).execute()
        except HttpError as exc:
            log.warning("Skipping status coloring: metadata fetch failed: %s", exc)
            return
        sheet_numeric_id: Optional[int] = None
        existing_values: set[str] = set()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") != self.tab_name:
                continue
            sheet_numeric_id = props.get("sheetId")
            for fmt in sheet.get("conditionalFormats", []) or []:
                rule = fmt.get("rule", {})
                cond = rule.get("booleanRule", {}).get("condition", {})
                if cond.get("type") != "TEXT_EQ":
                    continue
                ranges = rule.get("ranges", [])
                if not _covers_status_column(ranges, sheet_numeric_id):
                    continue
                for value in cond.get("values", []) or []:
                    if value.get("userEnteredValue"):
                        existing_values.add(value["userEnteredValue"])
        if sheet_numeric_id is None:
            log.warning("Skipping status coloring: tab %r not found", self.tab_name)
            return
        missing = [s for s in STATUS_COLORS if s.value not in existing_values]
        if not missing:
            return
        requests = status_format_requests(sheet_numeric_id, missing)
        log.info("Adding %d status color rule(s) to %r", len(requests), self.tab_name)
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": requests},
            ).execute()
        except HttpError as exc:
            log.warning("Skipping status coloring: batchUpdate failed: %s", exc)

    def list_rows(self) -> List[List[str]]:
        result = self._service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=self._range("A1:H"),
        ).execute()
        rows = result.get("values", []) or []
        # Pad short rows to match COLUMNS length so indexing is safe.
        width = len(COLUMNS)
        return [r + [""] * (width - len(r)) for r in rows]

    def find_row(self, company: str, role: str | None = None) -> Optional[int]:
        """Return 1-based row number (in the sheet, including header) of a matching
        company, or None. Matching is company-only and case-insensitive with
        whitespace normalized, so the same company never gets a second row for
        a different role. `role` is accepted for backward compatibility and
        ignored. Companies that normalize to "" or "unknown" never match."""
        rows = self.list_rows()
        c_target = _norm(company)
        if c_target in _UNMATCHABLE_COMPANIES:
            return None
        for idx, row in enumerate(rows[1:], start=2):  # skip header
            if len(row) < COL_INDEX["Company"]:
                continue
            if _norm(row[COL_INDEX["Company"] - 1]) == c_target:
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


def normalize_name(s: str) -> str:
    """Case-insensitive, whitespace-normalized comparison key for names."""
    return _norm(s)


def _hex_to_rgb(h: str) -> dict:
    h = h.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255.0,
        "green": int(h[2:4], 16) / 255.0,
        "blue": int(h[4:6], 16) / 255.0,
    }


def status_format_requests(
    sheet_numeric_id: int, statuses: Sequence[JobStatus] | None = None
) -> List[dict]:
    """Build addConditionalFormatRule requests coloring the Status column (E).

    Covers E2:E (header excluded) with one TEXT_EQ rule per status.
    """
    requests: List[dict] = []
    for status in STATUS_COLORS if statuses is None else statuses:
        bg, fg = STATUS_COLORS[status]
        requests.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_numeric_id,
                                "startRowIndex": 1,
                                "startColumnIndex": COL_INDEX["Status"] - 1,
                                "endColumnIndex": COL_INDEX["Status"],
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": status.value}],
                            },
                            "format": {
                                "backgroundColor": _hex_to_rgb(bg),
                                "textFormat": {
                                    "foregroundColor": _hex_to_rgb(fg),
                                    "bold": True,
                                },
                            },
                        },
                    },
                    "index": 0,
                }
            }
        )
    return requests


def _covers_status_column(ranges: Sequence[dict], sheet_numeric_id: int | None) -> bool:
    """True if any grid range covers the Status column (E) of our tab."""
    col = COL_INDEX["Status"] - 1
    for rng in ranges:
        if rng.get("sheetId") != sheet_numeric_id:
            continue
        start_col = rng.get("startColumnIndex", 0)
        end_col = rng.get("endColumnIndex", 0)
        if start_col <= col < (end_col or col + 1):
            return True
    return False


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
