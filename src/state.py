"""SQLite-backed state store: tracks last successful run and processed message ids.

Two tables:
- `state`         : key/value config (currently just `last_run_iso`)
- `processed_emails`: Gmail message ids we've already handled (idempotency)
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

_LAST_RUN_KEY = "last_run_iso"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_emails (
    message_id   TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    action       TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        log.debug("State DB ready at %s", self.db_path)

    def get_last_run(self) -> Optional[datetime]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE key = ?", (_LAST_RUN_KEY,)
            ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            log.warning("Corrupt last_run value %r; ignoring", row[0])
            return None

    def set_last_run(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        iso = when.astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_LAST_RUN_KEY, iso),
            )
        log.info("Updated last_run -> %s", iso)

    def is_processed(self, message_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row is not None

    def mark_processed(self, message_id: str, action: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_emails(message_id, processed_at, action) "
                "VALUES(?, ?, ?)",
                (message_id, datetime.now(timezone.utc).isoformat(), action),
            )

    def processed_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()
        return int(row[0]) if row else 0
