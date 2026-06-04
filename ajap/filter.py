from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FILTERS_PATH = Path("config/filters.json")


def load_filters() -> dict[str, list[str]]:
    if not FILTERS_PATH.exists():
        logger.warning("No filters.json at %s — keyword gates disabled", FILTERS_PATH)
        return {"whitelist": [], "blacklist": []}
    with FILTERS_PATH.open() as fh:
        return json.load(fh)


# Loaded once at module import; callers may override via the `filters` kwarg.
_default_filters: dict[str, list[str]] = load_filters()


def evaluate(job: dict, *, filters: dict | None = None) -> str:
    """Return 'QUEUED' or 'EVAL_REJECTED'."""
    return evaluate_with_reason(job, filters=filters)[0]


def evaluate_with_reason(
    job: dict, *, filters: dict | None = None
) -> tuple[str, str | None]:
    """Return (status, reason). reason is None when status is 'QUEUED'.

    Gates applied in order (cheapest first):
      1. ACTIVE  — 'active' key present and falsy → inactive
      2. BLACKLIST — any blacklist term in title → blacklist
      3. WHITELIST — no whitelist term in title → no-whitelist
    """
    _f = filters if filters is not None else _default_filters

    # Gate 1: active status (field absent ⇒ assume active, e.g. DB-row backfill)
    if "active" in job and not job["active"]:
        return "EVAL_REJECTED", "inactive"

    # Resolve title from either feed dict ('title') or DB row ('job_title')
    title = (job.get("title") or job.get("job_title") or "").lower()

    # Gate 2: blacklist takes priority over whitelist
    for term in _f.get("blacklist", []):
        if term.lower() in title:
            return "EVAL_REJECTED", "blacklist"

    # Gate 3: whitelist — must match at least one term
    whitelist = _f.get("whitelist", [])
    if whitelist and not any(term.lower() in title for term in whitelist):
        return "EVAL_REJECTED", "no-whitelist"

    return "QUEUED", None
