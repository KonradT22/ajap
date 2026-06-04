from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Exposed so tests can apply the schema to an in-memory connection directly.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_applications (
    job_hash             TEXT PRIMARY KEY,
    company_name         TEXT NOT NULL,
    job_title            TEXT NOT NULL,
    application_url      TEXT NOT NULL UNIQUE,
    career_track         TEXT
        CHECK(career_track IN ('DATA_ENGINEERING','MLOPS_MLE','GENERAL_SWE','IGNORE')),
    execution_status     TEXT NOT NULL
        CHECK(execution_status IN (
            'QUEUED','PENDING_EXECUTION','EVAL_REJECTED',
            'PROCESSING','CAPTCHA_BLOCKED','SESSION_TIMEOUT',
            'SUBMITTED','FAILED'
        )),
    retry_count          INTEGER NOT NULL DEFAULT 0,
    timestamp_discovered TEXT NOT NULL,
    timestamp_updated    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_status_hash
    ON job_applications(execution_status, job_hash);

CREATE TRIGGER IF NOT EXISTS trg_bump_updated
    AFTER UPDATE ON job_applications
    FOR EACH ROW
BEGIN
    UPDATE job_applications
       SET timestamp_updated = (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
     WHERE job_hash = NEW.job_hash;
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def insert_job(
    conn: sqlite3.Connection,
    *,
    job_hash: str,
    company_name: str,
    job_title: str,
    application_url: str,
) -> bool:
    """Insert a new QUEUED row. Returns True if inserted, False if already exists."""
    now = _now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO job_applications
            (job_hash, company_name, job_title, application_url,
             execution_status, timestamp_discovered, timestamp_updated)
        VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)
        """,
        (job_hash, company_name, job_title, application_url, now, now),
    )
    return cursor.rowcount == 1


def exists(conn: sqlite3.Connection, job_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM job_applications WHERE job_hash = ?", (job_hash,)
    ).fetchone()
    return row is not None


def get_next_pending(conn: sqlite3.Connection, limit: int = 1) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM job_applications WHERE execution_status = 'QUEUED' LIMIT ?",
        (limit,),
    ).fetchall()


def update_status(conn: sqlite3.Connection, job_hash: str, status: str) -> None:
    conn.execute(
        "UPDATE job_applications SET execution_status = ? WHERE job_hash = ?",
        (status, job_hash),
    )
