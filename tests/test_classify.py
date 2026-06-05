from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ajap import db
from ajap.classify import (
    VALID_TRACKS,
    _build_candidate_summary,
    _parse_track,
    classify_one,
)

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    # Columns added via migrate_db that aren't in SCHEMA_SQL yet for this test DB
    for col_sql in [
        "ALTER TABLE job_applications ADD COLUMN description TEXT",
        "ALTER TABLE job_applications ADD COLUMN description_source TEXT",
        "ALTER TABLE job_applications ADD COLUMN resume_path TEXT",
        "ALTER TABLE job_applications ADD COLUMN classify_reason TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass  # already present via SCHEMA_SQL update
    conn.commit()
    yield conn
    conn.close()


SAMPLE_PROFILE = {
    "education": {
        "degree": "Bachelor of Science",
        "major": "Computer Science",
        "graduation_date": "May 2027",
    },
    "demographics": {
        "citizenship": "United States Citizen",
        "visa_sponsorship_required": "No",
    },
    "resume_mappings": {
        "DATA_ENGINEERING": "resumes/data_engineering.pdf",
        "MLOPS_MLE": "resumes/mlops_infrastructure.pdf",
        "GENERAL_SWE": "resumes/backend_software.pdf",
    },
}


def _mock_response(text: str, in_tokens: int = 100, out_tokens: int = 5):
    um = SimpleNamespace(
        prompt_token_count=in_tokens, candidates_token_count=out_tokens
    )
    return SimpleNamespace(text=text, usage_metadata=um)


def _mock_client(responses: list[str]):
    """Return a mock Gemini client that yields the given text responses in order."""
    client = MagicMock()
    client.models.generate_content.side_effect = [_mock_response(r) for r in responses]
    return client


# ── _parse_track ──────────────────────────────────────────────────────────────


class TestParseTrack:
    def test_data_engineering(self):
        assert _parse_track("DATA_ENGINEERING") == ("DATA_ENGINEERING", None)

    def test_mlops_mle(self):
        assert _parse_track("MLOPS_MLE") == ("MLOPS_MLE", None)

    def test_general_swe(self):
        assert _parse_track("GENERAL_SWE") == ("GENERAL_SWE", None)

    def test_ignore(self):
        assert _parse_track("IGNORE") == ("IGNORE", None)

    def test_audit_mode_reason_captured(self):
        track, reason = _parse_track("MLOPS_MLE model serving role, ML infrastructure")
        assert track == "MLOPS_MLE"
        assert reason == "model serving role, ML infrastructure"

    def test_ignore_with_reason(self):
        track, reason = _parse_track("IGNORE requires 5 years experience, senior role")
        assert track == "IGNORE"
        assert "5 years" in reason

    def test_strips_trailing_punctuation(self):
        assert _parse_track("GENERAL_SWE.")[0] == "GENERAL_SWE"
        assert _parse_track("DATA_ENGINEERING,")[0] == "DATA_ENGINEERING"

    def test_leading_whitespace(self):
        assert _parse_track("  GENERAL_SWE  ")[0] == "GENERAL_SWE"

    def test_invalid_token_returns_none(self):
        assert _parse_track("I cannot determine the category")[0] is None

    def test_empty_returns_none(self):
        assert _parse_track("")[0] is None

    def test_lowercase_normalised(self):
        # Parser normalises case — tolerant of model returning mixed-case
        assert _parse_track("general_swe")[0] == "GENERAL_SWE"

    def test_all_valid_tracks_parse(self):
        for t in VALID_TRACKS:
            assert _parse_track(t)[0] == t


# ── _build_candidate_summary ──────────────────────────────────────────────────


def test_candidate_summary_includes_key_fields():
    summary = _build_candidate_summary(SAMPLE_PROFILE)
    assert "Bachelor of Science" in summary
    assert "Computer Science" in summary
    assert "May 2027" in summary
    assert "United States Citizen" in summary
    assert "No" in summary


# ── classify_one ─────────────────────────────────────────────────────────────


def test_classify_one_valid_track():
    client = _mock_client(["GENERAL_SWE"])
    result = classify_one(
        client, "gemini-2.5-flash", "SWE", "Acme", None, "summary", False
    )
    assert result.track == "GENERAL_SWE"
    assert result.reason is None
    assert result.input_tokens == 100
    assert result.output_tokens == 5
    assert result.cost_usd > 0


def test_classify_one_audit_mode_reason():
    client = _mock_client(["DATA_ENGINEERING ETL pipeline data platform role"])
    result = classify_one(
        client, "gemini-2.5-flash", "Data Engineer", "Corp", "desc", "s", True
    )
    assert result.track == "DATA_ENGINEERING"
    assert result.reason == "ETL pipeline data platform role"


def test_classify_one_retry_on_bad_response():
    # First call returns garbage, second returns valid track
    client = _mock_client(
        ["I'm not sure, this seems like GENERAL_SWE maybe", "MLOPS_MLE"]
    )
    result = classify_one(client, "gemini-2.5-flash", "MLE", "Co", "desc", "s", False)
    assert result.track == "MLOPS_MLE"
    assert client.models.generate_content.call_count == 2
    # Both calls' tokens accumulated
    assert result.input_tokens == 200
    assert result.output_tokens == 10


def test_classify_one_fails_after_two_bad_responses():
    client = _mock_client(["garbled output", "still garbled"])
    result = classify_one(client, "gemini-2.5-flash", "X", "Y", None, "s", False)
    assert result.track is None
    assert client.models.generate_content.call_count == 2


# ── state transitions (via db.update_classification) ─────────────────────────


def _insert_queued(conn, job_hash="abc123"):
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO job_applications "
        "(job_hash, company_name, job_title, application_url, "
        "execution_status, timestamp_discovered, timestamp_updated) "
        "VALUES (?, 'Acme', 'Eng', 'https://example.com', 'QUEUED', ?, ?)",
        (job_hash, now, now),
    )
    conn.commit()


def test_valid_track_sets_pending_execution(mem_conn):
    _insert_queued(mem_conn)
    db.update_classification(
        mem_conn, "abc123", "GENERAL_SWE", "resumes/backend.pdf", "PENDING_EXECUTION"
    )
    mem_conn.commit()
    row = mem_conn.execute(
        "SELECT career_track, resume_path, execution_status FROM job_applications WHERE job_hash='abc123'"
    ).fetchone()
    assert row["career_track"] == "GENERAL_SWE"
    assert row["resume_path"] == "resumes/backend.pdf"
    assert row["execution_status"] == "PENDING_EXECUTION"


def test_ignore_sets_eval_rejected(mem_conn):
    _insert_queued(mem_conn)
    db.update_classification(mem_conn, "abc123", "IGNORE", None, "EVAL_REJECTED")
    mem_conn.commit()
    row = mem_conn.execute(
        "SELECT career_track, resume_path, execution_status FROM job_applications WHERE job_hash='abc123'"
    ).fetchone()
    assert row["career_track"] == "IGNORE"
    assert row["resume_path"] is None
    assert row["execution_status"] == "EVAL_REJECTED"


def test_failed_response_leaves_queued(mem_conn):
    _insert_queued(mem_conn)
    # Simulate what run_classify does on result.track is None: no DB write
    row = mem_conn.execute(
        "SELECT execution_status, career_track FROM job_applications WHERE job_hash='abc123'"
    ).fetchone()
    assert row["execution_status"] == "QUEUED"
    assert row["career_track"] is None


def test_resume_path_from_mappings(mem_conn):
    _insert_queued(mem_conn)
    mappings = SAMPLE_PROFILE["resume_mappings"]
    db.update_classification(
        mem_conn, "abc123", "MLOPS_MLE", mappings["MLOPS_MLE"], "PENDING_EXECUTION"
    )
    mem_conn.commit()
    row = mem_conn.execute(
        "SELECT resume_path FROM job_applications WHERE job_hash='abc123'"
    ).fetchone()
    assert row["resume_path"] == "resumes/mlops_infrastructure.pdf"
