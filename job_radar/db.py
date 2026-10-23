import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job, Match

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT, country TEXT, url TEXT,
    status TEXT, reason TEXT, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    source TEXT, month TEXT, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (source, month)
);
CREATE TABLE IF NOT EXISTS emails (message_id TEXT PRIMARY KEY, processed_at TEXT);
CREATE TABLE IF NOT EXISTS scores (id TEXT PRIMARY KEY, match TEXT, scored_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Db:
    """SQLite state. In dry mode job/meta writes are skipped; API usage is always
    counted because dry runs still spend real quota."""

    def __init__(self, path: str | Path = "data/state.db", dry: bool = False):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.dry = dry
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def seen(self, job_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None

    def save(self, job: Job, status: str, reason: str = "") -> None:
        if self.dry:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (job.id, job.source, job.title, job.company, job.country, job.url, status, reason, now_iso()),
        )
        self.conn.commit()

    def notified(self) -> list[tuple[str, str, str]]:
        return self.conn.execute("SELECT company, title, country FROM jobs WHERE status='notified'").fetchall()

    def get_match(self, job_id: str) -> Match | None:
        row = self.conn.execute("SELECT match FROM scores WHERE id=?", (job_id,)).fetchone()
        return Match.model_validate_json(row[0]) if row else None

    def save_match(self, job_id: str, match: Match) -> None:
        if self.dry:
            return
        self.conn.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?)", (job_id, match.model_dump_json(), now_iso()))
        self.conn.commit()

    def email_seen(self, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM emails WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_email(self, message_id: str) -> None:
        if self.dry:
            return
        self.conn.execute("INSERT OR IGNORE INTO emails VALUES (?,?)", (message_id, now_iso()))
        self.conn.commit()

    def usage(self, source: str, month: str) -> int:
        row = self.conn.execute(
            "SELECT count FROM api_usage WHERE source=? AND month=?", (source, month)
        ).fetchone()
        return row[0] if row else 0

    def add_usage(self, source: str, month: str, n: int = 1) -> int:
        self.conn.execute(
            "INSERT INTO api_usage VALUES (?,?,?) "
            "ON CONFLICT(source, month) DO UPDATE SET count = count + excluded.count",
            (source, month, n),
        )
        self.conn.commit()
        return self.usage(source, month)

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        if self.dry:
            return
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        self.conn.commit()

    def record_health(self, source: str, ok: bool) -> int:
        """Track consecutive failing runs; returns the current streak."""
        streak = 0 if ok else int(self.get_meta(f"fails:{source}") or 0) + 1
        self.set_meta(f"fails:{source}", str(streak))
        return streak
