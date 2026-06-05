from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Exposed so tests can apply the schema to an in-memory connection directly.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_applications (
    job_hash             TEXT PRIMARY KEY,
    company_name         TEXT NOT NULL,
    job_title            TEXT NOT NULL,
    application_url      TEXT NOT NULL UNIQUE,
    source_id            TEXT,
    source_name          TEXT,
    role_type            TEXT NOT NULL DEFAULT 'new_grad'
        CHECK(role_type IN ('new_grad','internship')),
    is_active            INTEGER NOT NULL DEFAULT 1,
    date_posted          TEXT,
    locations_raw        TEXT,
    description          TEXT,
    description_source   TEXT,
    career_track         TEXT
        CHECK(career_track IN ('DATA_ENGINEERING','MLOPS_MLE','GENERAL_SWE','IGNORE')),
    resume_path          TEXT,
    classify_reason      TEXT,
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


def migrate_db(db_path: str) -> None:
    """Add any columns present in SCHEMA_SQL but missing from the live table.
    Safe to call on an already-current DB — no-ops if nothing is missing.
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(job_applications)")
        }
        if "description" not in existing:
            conn.execute("ALTER TABLE job_applications ADD COLUMN description TEXT")
            logger.info("Schema migration: added 'description' column")
        if "description_source" not in existing:
            conn.execute(
                "ALTER TABLE job_applications ADD COLUMN description_source TEXT"
            )
            logger.info("Schema migration: added 'description_source' column")
        if "resume_path" not in existing:
            conn.execute("ALTER TABLE job_applications ADD COLUMN resume_path TEXT")
            logger.info("Schema migration: added 'resume_path' column")
        if "classify_reason" not in existing:
            conn.execute(
                "ALTER TABLE job_applications ADD COLUMN classify_reason TEXT"
            )
            logger.info("Schema migration: added 'classify_reason' column")
        if "locations_raw" not in existing:
            conn.execute(
                "ALTER TABLE job_applications ADD COLUMN locations_raw TEXT"
            )
            logger.info("Schema migration: added 'locations_raw' column")
        if "source_name" not in existing:
            conn.execute("ALTER TABLE job_applications ADD COLUMN source_name TEXT")
            logger.info("Schema migration: added 'source_name' column")
        if "role_type" not in existing:
            conn.execute(
                "ALTER TABLE job_applications ADD COLUMN role_type TEXT NOT NULL DEFAULT 'new_grad'"
            )
            # Backfill: all pre-existing rows came from the new-grad feed.
            conn.execute("UPDATE job_applications SET role_type = 'new_grad'")
            logger.info("Schema migration: added 'role_type' column, backfilled as 'new_grad'")


def insert_job(
    conn: sqlite3.Connection,
    *,
    job_hash: str,
    company_name: str,
    job_title: str,
    application_url: str,
    source_id: str | None = None,
    source_name: str | None = None,
    role_type: str = "new_grad",
    is_active: int = 1,
    date_posted: str | None = None,
    locations_raw: str | None = None,
    status: str = "QUEUED",
) -> bool:
    """Insert a new row. Returns True if inserted, False if hash or URL already exists."""
    now = _now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO job_applications
            (job_hash, company_name, job_title, application_url,
             source_id, source_name, role_type, is_active, date_posted, locations_raw,
             execution_status, timestamp_discovered, timestamp_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_hash,
            company_name,
            job_title,
            application_url,
            source_id,
            source_name,
            role_type,
            is_active,
            date_posted,
            locations_raw,
            status,
            now,
            now,
        ),
    )
    return cursor.rowcount == 1


def reconcile_active(
    conn: sqlite3.Connection,
    job_hash: str,
    is_active: int,
) -> bool:
    """
    Update is_active on a known row. If it went inactive while QUEUED,
    demote to EVAL_REJECTED. Returns True if the row was demoted.
    """
    row = conn.execute(
        "SELECT is_active, execution_status FROM job_applications WHERE job_hash = ?",
        (job_hash,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE job_applications SET is_active = ? WHERE job_hash = ?",
        (is_active, job_hash),
    )
    if not is_active and row["execution_status"] == "QUEUED":
        update_status(conn, job_hash, "EVAL_REJECTED")
        return True
    return False


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


def update_classification(
    conn: sqlite3.Connection,
    job_hash: str,
    career_track: str,
    resume_path: str | None,
    status: str,
    reason: str | None = None,
) -> None:
    conn.execute(
        "UPDATE job_applications "
        "SET career_track = ?, resume_path = ?, execution_status = ?, classify_reason = ? "
        "WHERE job_hash = ?",
        (career_track, resume_path, status, reason, job_hash),
    )


def update_description(
    conn: sqlite3.Connection,
    job_hash: str,
    description: str | None,
    source: str | None = None,
) -> None:
    conn.execute(
        "UPDATE job_applications SET description = ?, description_source = ? WHERE job_hash = ?",
        (description, source, job_hash),
    )
