from __future__ import annotations


from ajap.filter import evaluate, evaluate_with_reason

# Deterministic filters for all tests — never read from disk.
F = {
    "whitelist": ["New Grad", "Class of 2027", "University Graduate", "Early Career"],
    "blacklist": ["Summer 2027", "Class of 2028", "3+ years", "Senior", "Lead"],
}


# ── V&V matrix boundary case ──────────────────────────────────────────────────


def test_blacklist_wins_over_whitelist():
    """Title containing both a blacklist and a whitelist term must be rejected."""
    job = {"title": "New Grad SWE Summer 2027", "active": True}
    assert evaluate(job, filters=F) == "EVAL_REJECTED"
    _, reason = evaluate_with_reason(job, filters=F)
    assert reason == "blacklist"


# ── gate 1: active ────────────────────────────────────────────────────────────


def test_inactive_listing_rejected():
    job = {"title": "New Grad Software Engineer", "active": False}
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "inactive"


def test_missing_active_field_treated_as_active():
    """DB-row backfill has no 'active' key — must not reject on that gate."""
    job = {"job_title": "New Grad Software Engineer"}
    assert evaluate(job, filters=F) == "QUEUED"


# ── gate 2: blacklist ─────────────────────────────────────────────────────────


def test_blacklist_term_in_title_rejected():
    job = {"title": "Senior Software Engineer", "active": True}
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "blacklist"


def test_blacklist_case_insensitive():
    job = {"title": "SENIOR data engineer", "active": True}
    assert evaluate(job, filters=F) == "EVAL_REJECTED"


def test_lead_in_title_rejected():
    job = {"title": "Tech Lead - Platform", "active": True}
    assert evaluate(job, filters=F) == "EVAL_REJECTED"


# ── gate 3: whitelist ─────────────────────────────────────────────────────────


def test_no_whitelist_term_rejected():
    job = {"title": "Staff Software Engineer", "active": True}
    status, reason = evaluate_with_reason(job, filters=F)
    assert status == "EVAL_REJECTED"
    assert reason == "no-whitelist"


def test_whitelist_case_insensitive():
    job = {"title": "new grad data engineer", "active": True}
    assert evaluate(job, filters=F) == "QUEUED"


def test_db_row_job_title_field():
    """evaluate() must accept DB-style 'job_title' key, not just feed 'title'."""
    job = {"job_title": "New Grad Software Engineer"}
    assert evaluate(job, filters=F) == "QUEUED"


# ── happy path ────────────────────────────────────────────────────────────────


def test_clean_new_grad_active_passes():
    job = {"title": "New Grad Software Engineer", "active": True}
    assert evaluate(job, filters=F) == "QUEUED"


def test_early_career_active_passes():
    job = {"title": "Early Career Data Engineer", "active": True}
    assert evaluate(job, filters=F) == "QUEUED"


def test_class_of_2027_active_passes():
    job = {"title": "Software Engineer — Class of 2027", "active": True}
    assert evaluate(job, filters=F) == "QUEUED"


# ── empty-filter edge cases ───────────────────────────────────────────────────


def test_empty_whitelist_passes_everything():
    """When whitelist is empty, gate 3 is disabled — no false rejections."""
    job = {"title": "Some Random Title", "active": True}
    assert evaluate(job, filters={"whitelist": [], "blacklist": []}) == "QUEUED"


def test_blacklist_still_fires_with_empty_whitelist():
    job = {"title": "Senior Engineer", "active": True}
    assert (
        evaluate(job, filters={"whitelist": [], "blacklist": ["Senior"]})
        == "EVAL_REJECTED"
    )
