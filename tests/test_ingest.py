from __future__ import annotations

import hashlib
import sqlite3

import pytest

from ajap import db
from ajap.ingest import _job_hash

UUID = "18562b19-ab94-4493-833e-3e5df157c67b"


@pytest.fixture
def mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    return conn


# ── hash behaviour ────────────────────────────────────────────────────────────


def test_hash_uses_source_id_when_present():
    h = _job_hash(UUID, "Acme Corp", "SWE", "https://example.com")
    assert h == hashlib.sha256(UUID.encode()).hexdigest()


def test_hash_source_id_ignores_other_fields():
    h1 = _job_hash(UUID, "Acme Corp", "SWE", "https://example.com")
    h2 = _job_hash(UUID, "DIFFERENT", "TITLE", "https://other.com")
    assert h1 == h2


def test_hash_fallback_without_source_id():
    h1 = _job_hash(None, "Acme Corp", "SWE", "https://example.com")
    h2 = _job_hash("", "Acme Corp", "SWE", "https://example.com")
    assert h1 == h2
    assert h1 == hashlib.sha256(b"Acme CorpSWEhttps://example.com").hexdigest()


def test_hash_fallback_determinism():
    h1 = _job_hash(None, "Acme Corp", "Software Engineer", "https://example.com/job")
    h2 = _job_hash(None, "Acme Corp", "Software Engineer", "https://example.com/job")
    assert h1 == h2


def test_hash_fallback_differs_by_url():
    h1 = _job_hash(None, "Acme Corp", "SWE", "https://example.com/1")
    h2 = _job_hash(None, "Acme Corp", "SWE", "https://example.com/2")
    assert h1 != h2


def test_hash_fallback_case_sensitive():
    h1 = _job_hash(None, "Acme", "SWE", "https://example.com")
    h2 = _job_hash(None, "acme", "SWE", "https://example.com")
    assert h1 != h2


# ── deduplication ─────────────────────────────────────────────────────────────


def test_dedup_same_hash(mem_conn):
    h = _job_hash(UUID, "TestCo", "Junior SWE", "https://example.com/1")
    first = db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="TestCo",
        job_title="Junior SWE",
        application_url="https://example.com/1",
        source_id=UUID,
    )
    second = db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="TestCo",
        job_title="Junior SWE",
        application_url="https://example.com/1",
        source_id=UUID,
    )
    assert first is True
    assert second is False
    count = mem_conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    assert count == 1


def test_dedup_same_url_different_hash(mem_conn):
    h1 = _job_hash("uuid-aaa", "Co A", "Role X", "https://example.com/shared")
    h2 = _job_hash("uuid-bbb", "Co B", "Role Y", "https://example.com/shared")
    db.insert_job(
        mem_conn,
        job_hash=h1,
        company_name="Co A",
        job_title="Role X",
        application_url="https://example.com/shared",
        source_id="uuid-aaa",
    )
    second = db.insert_job(
        mem_conn,
        job_hash=h2,
        company_name="Co B",
        job_title="Role Y",
        application_url="https://example.com/shared",
        source_id="uuid-bbb",
    )
    assert second is False
    count = mem_conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    assert count == 1


# ── exists ────────────────────────────────────────────────────────────────────


def test_exists_false_before_insert(mem_conn):
    assert (
        db.exists(mem_conn, _job_hash("no-such-id", "Ghost", "Role", "http://x"))
        is False
    )


def test_exists_true_after_insert(mem_conn):
    h = _job_hash("uuid-wc", "WidgetCo", "ML Engineer", "https://example.com/2")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="WidgetCo",
        job_title="ML Engineer",
        application_url="https://example.com/2",
        source_id="uuid-wc",
    )
    assert db.exists(mem_conn, h) is True


# ── reconcile_active ──────────────────────────────────────────────────────────


def test_reconcile_active_demotes_queued(mem_conn):
    h = _job_hash("uuid-rc", "BigTech", "SWE", "https://example.com/3")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="BigTech",
        job_title="SWE",
        application_url="https://example.com/3",
        source_id="uuid-rc",
        is_active=1,
    )
    demoted = db.reconcile_active(mem_conn, h, is_active=0)
    assert demoted is True
    row = mem_conn.execute(
        "SELECT execution_status FROM job_applications WHERE job_hash = ?", (h,)
    ).fetchone()
    assert row["execution_status"] == "EVAL_REJECTED"


def test_reconcile_active_no_demotion_when_not_queued(mem_conn):
    h = _job_hash("uuid-nd", "Co", "Role", "https://example.com/4")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="Co",
        job_title="Role",
        application_url="https://example.com/4",
        source_id="uuid-nd",
        is_active=1,
        status="EVAL_REJECTED",
    )
    demoted = db.reconcile_active(mem_conn, h, is_active=0)
    assert demoted is False
    row = mem_conn.execute(
        "SELECT execution_status FROM job_applications WHERE job_hash = ?", (h,)
    ).fetchone()
    assert row["execution_status"] == "EVAL_REJECTED"


# ── status & existing tests ───────────────────────────────────────────────────


def test_initial_status_is_queued(mem_conn):
    h = _job_hash("uuid-bt", "BigTech", "Data Engineer", "https://example.com/5")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="BigTech",
        job_title="Data Engineer",
        application_url="https://example.com/5",
        source_id="uuid-bt",
    )
    row = mem_conn.execute(
        "SELECT execution_status FROM job_applications WHERE job_hash = ?", (h,)
    ).fetchone()
    assert row["execution_status"] == "QUEUED"


def test_update_status(mem_conn):
    h = _job_hash("uuid-us", "BigTech", "Data Engineer", "https://example.com/6")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="BigTech",
        job_title="Data Engineer",
        application_url="https://example.com/6",
        source_id="uuid-us",
    )
    db.update_status(mem_conn, h, "EVAL_REJECTED")
    row = mem_conn.execute(
        "SELECT execution_status FROM job_applications WHERE job_hash = ?", (h,)
    ).fetchone()
    assert row["execution_status"] == "EVAL_REJECTED"


def test_eval_rejected_row_not_deleted(mem_conn):
    h = _job_hash("uuid-er", "OldCo", "Ancient Role", "https://example.com/7")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="OldCo",
        job_title="Ancient Role",
        application_url="https://example.com/7",
        source_id="uuid-er",
    )
    db.update_status(mem_conn, h, "EVAL_REJECTED")
    count = mem_conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    assert count == 1
