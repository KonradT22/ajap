from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ajap.filter import evaluate, evaluate_with_reason

# Deterministic filters for all tests — never read from disk.
F = {
    "recency_days": 45,
    "whitelist": ["New Grad", "Class of 2027", "University Graduate", "Early Career"],
    "blacklist": ["Summer 2027", "Class of 2028", "3+ years", "Senior", "Lead"],
}

F_NO_RECENCY = {**F, "recency_days": 0}


def _days_ago(n: int) -> str:
    """ISO timestamp string for n days in the past."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _unix_days_ago(n: int) -> float:
    return (datetime.now(timezone.utc) - timedelta(days=n)).timestamp()


# ── gate 1: active ────────────────────────────────────────────────────────────


def test_inactive_feed_dict_rejected():
    job = {"title": "New Grad Software Engineer", "active": False}
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "inactive"


def test_inactive_db_row_rejected():
    job = {
        "job_title": "New Grad Software Engineer",
        "is_active": 0,
        "date_posted": _days_ago(5),
    }
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "inactive"


def test_active_db_row_not_rejected_by_gate1():
    job = {
        "job_title": "New Grad Software Engineer",
        "is_active": 1,
        "date_posted": _days_ago(5),
    }
    assert evaluate(job, filters=F) == "QUEUED"


def test_missing_active_field_treated_as_active():
    """DB-row backfill with no active/is_active key — gate 1 must not fire."""
    job = {"job_title": "New Grad Software Engineer"}
    assert evaluate(job, filters=F_NO_RECENCY) == "QUEUED"


# ── gate 2: recency ───────────────────────────────────────────────────────────


def test_recent_listing_passes_recency_iso():
    job = {"title": "New Grad SWE", "active": True, "date_posted": _days_ago(10)}
    assert evaluate(job, filters=F) == "QUEUED"


def test_recent_listing_passes_recency_unix():
    job = {"title": "New Grad SWE", "active": True, "date_posted": _unix_days_ago(10)}
    assert evaluate(job, filters=F) == "QUEUED"


def test_old_listing_rejected_iso():
    job = {"title": "New Grad SWE", "active": True, "date_posted": _days_ago(60)}
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "too-old"


def test_old_listing_rejected_unix():
    job = {"title": "New Grad SWE", "active": True, "date_posted": _unix_days_ago(60)}
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "too-old"


def test_no_date_skips_recency():
    """A listing with no date_posted must not be rejected on recency."""
    job = {"title": "New Grad SWE", "active": True}
    assert evaluate(job, filters=F) == "QUEUED"


def test_recency_days_zero_disables_gate():
    job = {"title": "New Grad SWE", "active": True, "date_posted": _days_ago(500)}
    assert evaluate(job, filters=F_NO_RECENCY) == "QUEUED"


# ── gate 3: blacklist ─────────────────────────────────────────────────────────


def test_blacklist_wins_over_whitelist():
    """V&V boundary: title with both a blacklist AND a whitelist term → rejected."""
    job = {
        "title": "New Grad SWE Summer 2027",
        "active": True,
        "date_posted": _days_ago(5),
    }
    assert evaluate(job, filters=F) == "EVAL_REJECTED"
    _, reason = evaluate_with_reason(job, filters=F)
    assert reason == "blacklist"


def test_blacklist_case_insensitive():
    job = {"title": "SENIOR data engineer", "active": True, "date_posted": _days_ago(5)}
    assert evaluate(job, filters=F) == "EVAL_REJECTED"


def test_lead_rejected():
    job = {"title": "Tech Lead - Platform", "active": True, "date_posted": _days_ago(5)}
    assert evaluate(job, filters=F) == "EVAL_REJECTED"


# ── gate 4: whitelist ─────────────────────────────────────────────────────────


def test_no_whitelist_term_rejected():
    job = {
        "title": "Staff Software Engineer",
        "active": True,
        "date_posted": _days_ago(5),
    }
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "no-whitelist"


def test_whitelist_case_insensitive():
    job = {
        "title": "new grad data engineer",
        "active": True,
        "date_posted": _days_ago(5),
    }
    assert evaluate(job, filters=F) == "QUEUED"


def test_db_row_job_title_field():
    """evaluate() must accept DB-style 'job_title', not just feed 'title'."""
    job = {"job_title": "New Grad SWE", "is_active": 1, "date_posted": _days_ago(5)}
    assert evaluate(job, filters=F) == "QUEUED"


# ── happy path ────────────────────────────────────────────────────────────────


def test_clean_new_grad_passes():
    job = {
        "title": "New Grad Software Engineer",
        "active": True,
        "date_posted": _days_ago(5),
    }
    assert evaluate(job, filters=F) == "QUEUED"


def test_early_career_passes():
    job = {
        "title": "Early Career Data Engineer",
        "active": True,
        "date_posted": _days_ago(5),
    }
    assert evaluate(job, filters=F) == "QUEUED"


def test_class_of_2027_passes():
    job = {
        "title": "Software Engineer — Class of 2027",
        "active": True,
        "date_posted": _days_ago(5),
    }
    assert evaluate(job, filters=F) == "QUEUED"


# ── empty-filter edge cases ───────────────────────────────────────────────────


def test_empty_whitelist_passes_everything():
    job = {"title": "Some Random Title", "active": True, "date_posted": _days_ago(5)}
    assert (
        evaluate(job, filters={"recency_days": 0, "whitelist": [], "blacklist": []})
        == "QUEUED"
    )


def test_blacklist_fires_with_empty_whitelist():
    job = {"title": "Senior Engineer", "active": True, "date_posted": _days_ago(5)}
    assert (
        evaluate(
            job, filters={"recency_days": 0, "whitelist": [], "blacklist": ["Senior"]}
        )
        == "EVAL_REJECTED"
    )
