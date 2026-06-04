from __future__ import annotations

import hashlib
import sqlite3

import pytest

from ajap import db
from ajap.ingest import _job_hash


@pytest.fixture
def mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    return conn


# ── hash behaviour ────────────────────────────────────────────────────────────


def test_hash_determinism():
    h1 = _job_hash("Acme Corp", "Software Engineer")
    h2 = _job_hash("Acme Corp", "Software Engineer")
    assert h1 == h2
    assert h1 == hashlib.sha256(b"Acme CorpSoftware Engineer").hexdigest()


def test_hash_differs_by_title():
    assert _job_hash("Acme Corp", "SWE") != _job_hash("Acme Corp", "Data Engineer")


def test_hash_case_sensitive():
    assert _job_hash("Acme", "SWE") != _job_hash("acme", "SWE")


# ── deduplication ─────────────────────────────────────────────────────────────


def test_dedup_same_hash(mem_conn):
    h = _job_hash("TestCo", "Junior SWE")
    first = db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="TestCo",
        job_title="Junior SWE",
        application_url="https://example.com/1",
    )
    second = db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="TestCo",
        job_title="Junior SWE",
        application_url="https://example.com/1",
    )
    assert first is True
    assert second is False
    count = mem_conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    assert count == 1


def test_dedup_same_url_different_hash(mem_conn):
    h1 = _job_hash("Co A", "Role X")
    h2 = _job_hash("Co B", "Role Y")
    db.insert_job(
        mem_conn,
        job_hash=h1,
        company_name="Co A",
        job_title="Role X",
        application_url="https://example.com/shared",
    )
    second = db.insert_job(
        mem_conn,
        job_hash=h2,
        company_name="Co B",
        job_title="Role Y",
        application_url="https://example.com/shared",
    )
    # UNIQUE on application_url: second insert must be silently skipped
    assert second is False
    count = mem_conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    assert count == 1


# ── exists ────────────────────────────────────────────────────────────────────


def test_exists_false_before_insert(mem_conn):
    assert db.exists(mem_conn, _job_hash("Ghost Co", "Phantom Role")) is False


def test_exists_true_after_insert(mem_conn):
    h = _job_hash("WidgetCo", "ML Engineer")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="WidgetCo",
        job_title="ML Engineer",
        application_url="https://example.com/2",
    )
    assert db.exists(mem_conn, h) is True


# ── status & trigger ──────────────────────────────────────────────────────────


def test_initial_status_is_queued(mem_conn):
    h = _job_hash("BigTech", "Data Engineer")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="BigTech",
        job_title="Data Engineer",
        application_url="https://example.com/3",
    )
    row = mem_conn.execute(
        "SELECT execution_status FROM job_applications WHERE job_hash = ?", (h,)
    ).fetchone()
    assert row["execution_status"] == "QUEUED"


def test_update_status(mem_conn):
    h = _job_hash("BigTech", "Data Engineer")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="BigTech",
        job_title="Data Engineer",
        application_url="https://example.com/4",
    )
    db.update_status(mem_conn, h, "EVAL_REJECTED")
    row = mem_conn.execute(
        "SELECT execution_status FROM job_applications WHERE job_hash = ?", (h,)
    ).fetchone()
    assert row["execution_status"] == "EVAL_REJECTED"


def test_eval_rejected_row_not_deleted(mem_conn):
    h = _job_hash("OldCo", "Ancient Role")
    db.insert_job(
        mem_conn,
        job_hash=h,
        company_name="OldCo",
        job_title="Ancient Role",
        application_url="https://example.com/5",
    )
    db.update_status(mem_conn, h, "EVAL_REJECTED")
    count = mem_conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    assert count == 1
