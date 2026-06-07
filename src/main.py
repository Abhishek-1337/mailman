"""Cron entrypoint: Gmail -> OpenAI -> Google Sheets pipeline."""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, time, timedelta, timezone
from email.utils import getaddresses

from .config import Config, configure_logging, load_config
from .gmail_client import GmailClient
from .llm import JobExtractor
from .models import EmailMessage, JobAction, JobApplication, JobStatus, LLMResult
from .sheets_client import (
    COLUMNS,
    SheetsClient,
    app_from_parts,
    row_to_app,
)
from .state import StateStore

log = logging.getLogger("mailman")

CONFIDENCE_THRESHOLD = 0.5


def run() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)
    log.info("=" * 60)
    log.info("Mailman run starting (dry_run=%s)", cfg.dry_run)

    state = StateStore(cfg.state_db_path)
    last_run = state.get_last_run()
    cutoff = _compute_cutoff(last_run, cfg.lookback_hours)
    log.info("Looking at emails since %s", cutoff.isoformat())

    try:
        gmail = GmailClient(cfg.gmail_credentials_path, cfg.gmail_token_path)
    except FileNotFoundError as exc:
        log.error(str(exc))
        return 2

    try:
        sheets = SheetsClient(
            sheet_id=cfg.sheet_id,
            tab_name=cfg.sheet_tab_name,
            credentials_path=cfg.sheets_credentials_path,
            token_path=cfg.sheets_token_path,
        )
    except FileNotFoundError as exc:
        log.error(str(exc))
        return 2

    sheets.ensure_tab()

    messages = gmail.list_messages(since=cutoff, max_results=100)
    if not messages:
        log.info("No candidate messages; exiting cleanly")
        _mark_run(state, cutoff)
        return 0

    extractor = JobExtractor(cfg.openai_api_key, cfg.openai_model)

    new_count = 0
    updated_count = 0
    skipped_count = 0

    for msg in messages:
        try:
            outcome = _process_message(
                msg=msg,
                state=state,
                extractor=extractor,
                sheets=sheets,
                dry_run=cfg.dry_run,
            )
        except Exception:
            log.exception("Unhandled error processing %s: %s", msg.message_id, traceback.format_exc())
            continue

        if outcome == "new":
            new_count += 1
        elif outcome == "updated":
            updated_count += 1
        else:
            skipped_count += 1

    log.info(
        "Run summary: new=%d updated=%d skipped=%d total_seen=%d",
        new_count, updated_count, skipped_count, len(messages),
    )

    # Advance the run cursor: use the most recent message date so we don't
    # repeatedly re-process mail that arrived during the run.
    if messages:
        latest = max(m.date for m in messages)
        _mark_run(state, latest)
    else:
        _mark_run(state, cutoff)
    return 0


def _process_message(
    *,
    msg: EmailMessage,
    state: StateStore,
    extractor: JobExtractor,
    sheets: SheetsClient,
    dry_run: bool,
) -> str:
    """Returns 'new' | 'updated' | 'skipped'."""
    if state.is_processed(msg.message_id):
        log.debug("Skip %s: already processed", msg.message_id)
        return "skipped"

    log.info("Classifying msg=%s subject=%r", msg.message_id, msg.subject[:80])
    result = extractor.extract(msg)
    if result is None:
        log.info("Skip %s: LLM extraction failed", msg.message_id)
        state.mark_processed(msg.message_id, action="extract_failed")
        return "skipped"

    if not result.is_job_related or result.action == JobAction.IRRELEVANT:
        log.info("Skip %s: not job-related (%s)", msg.message_id, result.reasoning)
        state.mark_processed(msg.message_id, action="irrelevant")
        return "skipped"

    if result.confidence < CONFIDENCE_THRESHOLD:
        log.info("Skip %s: low confidence %.2f (%s)", msg.message_id, result.confidence, result.reasoning)
        state.mark_processed(msg.message_id, action="low_confidence")
        return "skipped"

    platform = result.platform or _platform_from_sender(msg.sender)
    app = _build_app(msg, result, platform)
    row_idx = sheets.find_row(app.company, app.role)

    if dry_run:
        action = "would_update" if row_idx else "would_append"
        log.info("[DRY-RUN] %s row=%s for %s @ %s", action, row_idx, app.role, app.company)
        state.mark_processed(msg.message_id, action=action)
        return "updated" if row_idx else "new"

    if row_idx:
        existing = row_to_app(sheets.list_rows()[row_idx - 1])
        # Preserve original Date Applied; only refresh status/last_update/notes/subject
        merged = JobApplication(
            date_applied=existing.date_applied or app.date_applied,
            company=existing.company or app.company,
            role=existing.role or app.role,
            platform=existing.platform or app.platform,
            status=_bump_status(existing.status, app.status),
            last_update=app.last_update,
            notes=_append_note(existing.notes, result.reasoning),
            email_subject=app.email_subject or existing.email_subject,
        )
        sheets.update(row_idx, merged)
        state.mark_processed(msg.message_id, action="updated")
        return "updated"

    sheets.append(app)
    state.mark_processed(msg.message_id, action="appended")
    return "new"


def _build_app(msg: EmailMessage, result: LLMResult, platform: str) -> JobApplication:
    date_str = msg.date.astimezone().strftime("%Y-%m-%d %H:%M")
    return app_from_parts(
        date_applied=date_str,
        company=result.company or "Unknown",
        role=result.role or "Unknown",
        platform=platform or "Unknown",
        status=result.status or JobStatus.UNKNOWN,
        last_update=date_str,
        notes=f"From: {msg.sender} | {result.reasoning}"[:500],
        email_subject=msg.subject[:200],
    )


def _platform_from_sender(sender: str) -> str:
    domain_map = {
        "linkedin.com": "LinkedIn",
        "indeed.com": "Indeed",
        "glassdoor.com": "Glassdoor",
        "wellfound.com": "Wellfound",
        "angel.co": "Wellfound",
        "greenhouse.io": "Greenhouse",
        "lever.co": "Lever",
        "ashbyhq.com": "Ashby",
        "myworkday.com": "Workday",
        "myworkdayjobs.com": "Workday",
        "smartrecruiters.com": "SmartRecruiters",
        "icims.com": "iCIMS",
        "jobvite.com": "Jobvite",
        "bamboohr.com": "BambooHR",
        "workable.com": "Workable",
        "taleo.net": "Taleo",
        "teamtailor.com": "Teamtailor",
    }
    addrs = getaddresses([sender])
    if not addrs:
        return "Unknown"
    _, addr = addrs[0]
    if "@" not in addr:
        return "Unknown"
    domain = addr.split("@", 1)[1].lower()
    for needle, name in domain_map.items():
        if domain == needle or domain.endswith("." + needle):
            return name
    # Use the registrable domain as a fallback
    return domain.split(".")[-2].capitalize() if "." in domain else domain


def _bump_status(existing: JobStatus, incoming: JobStatus) -> JobStatus:
    """Pick the more informative of two statuses (don't regress to 'Applied' once rejected, etc.)."""
    order = [
        JobStatus.UNKNOWN,
        JobStatus.APPLIED,
        JobStatus.ASSESSMENT,
        JobStatus.SHORTLISTED,
        JobStatus.INTERVIEW,
        JobStatus.OFFER,
        JobStatus.WITHDRAWN,
        JobStatus.REJECTED,
    ]
    try:
        return incoming if order.index(incoming) > order.index(existing) else existing
    except ValueError:
        return incoming


def _append_note(existing: str, addition: str) -> str:
    addition = (addition or "").strip()
    if not addition:
        return existing
    if addition in existing:
        return existing
    if existing:
        return f"{existing} | {addition}"[:500]
    return addition[:500]


def _compute_cutoff(last_run: datetime | None, lookback_hours: int) -> datetime:
    now = datetime.now(timezone.utc)
    if last_run is None:
        # First run: only consider today's mail.
        return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return max(last_run, now - timedelta(hours=lookback_hours))


def _mark_run(state: StateStore, when: datetime) -> None:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    state.set_last_run(when)


def main() -> int:
    try:
        return run()
    except Exception:
        log.exception("Fatal error: %s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
