"""Gmail API wrapper: OAuth flow, search, and message normalization."""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Iterable, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import EmailMessage

log = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _ensure_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    """Load existing token, refresh if needed, otherwise run the local auth flow."""
    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing Gmail access token")
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds or not creds.valid:
        if os.getenv("CI"):
            raise RuntimeError(
                f"Gmail token at {token_path} is invalid or missing and could not be refreshed. "
                "Run scripts/auth_gmail.py locally to generate a fresh token, "
                "then base64-encode the file and update the GMAIL_TOKEN_JSON secret."
            )
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Gmail credentials.json not found at {credentials_path}. "
                "Download it from the Google Cloud Console (OAuth client, Desktop app)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        log.info("Saved Gmail token to %s", token_path)
    return creds


class GmailClient:
    def __init__(self, credentials_path: Path, token_path: Path) -> None:
        creds = _ensure_credentials(credentials_path, token_path)
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def list_messages(
        self,
        since: Optional[datetime] = None,
        max_results: int = 100,
        extra_query: Optional[str] = None,
    ) -> List[EmailMessage]:
        """Return up to `max_results` messages. If `since` is provided, only
        messages at/after that date are returned. Otherwise fetches the latest
        `max_results` messages regardless of date.

        `extra_query` lets callers add Gmail search operators (e.g. subject filters).
        """
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            # Gmail's `after:` operator uses YYYY/MM/DD and is inclusive.
            date_part = f"after:{since.strftime('%Y/%m/%d')}"
            query = f"{date_part} {extra_query}".strip() if extra_query else date_part
        else:
            query = extra_query or ""

        log.info("Gmail search query: %r", query)
        try:
            resp = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            log.error("Gmail list failed: %s", exc)
            raise

        refs = resp.get("messages", []) or []
        log.info("Found %d candidate messages", len(refs))
        return [self._fetch(msg["id"], msg.get("threadId", "")) for msg in refs]

    def _fetch(self, message_id: str, thread_id: str) -> EmailMessage:
        full = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = full.get("payload", {}) or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        date_hdr = headers.get("date", "")
        try:
            date = parsedate_to_datetime(date_hdr) if date_hdr else datetime.now(timezone.utc)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            date = datetime.now(timezone.utc)

        body = self._extract_body(payload)
        return EmailMessage(
            message_id=message_id,
            thread_id=thread_id or full.get("threadId", ""),
            sender=sender,
            subject=subject,
            date=date,
            body_text=body,
        )

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Walk MIME parts and return the first usable text/plain or text/html body."""
        def _decode(data: Optional[str]) -> str:
            if not data:
                return ""
            try:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                return ""

        def _walk(part: dict) -> Iterable[str]:
            mime = (part.get("mimeType") or "").lower()
            if mime == "text/plain" and part.get("body", {}).get("data"):
                yield _decode(part["body"]["data"])
                return
            if mime == "text/html" and part.get("body", {}).get("data"):
                yield _decode(part["body"]["data"])
                return
            for sub in part.get("parts", []) or []:
                yield from _walk(sub)

        for chunk in _walk(payload):
            if chunk:
                return _strip_html(chunk) if "<" in chunk and ">" in chunk else chunk
        return ""

    @staticmethod
    def sender_domain(sender: str) -> str:
        _, addr = getaddresses([sender])[0] if getaddresses([sender]) else ("", "")
        if "@" in addr:
            return addr.split("@", 1)[1].lower()
        return ""


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
