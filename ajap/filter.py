from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FILTERS_PATH = Path("config/filters.json")


def load_filters() -> dict:
    if not FILTERS_PATH.exists():
        logger.warning("No filters.json at %s — keyword gates disabled", FILTERS_PATH)
        return {"recency_days": 45, "whitelist": [], "blacklist": []}
    with FILTERS_PATH.open() as fh:
        return json.load(fh)


# Loaded once at module import; callers may override via the `filters` kwarg.
_default_filters: dict = load_filters()


def evaluate(job: dict, *, filters: dict | None = None) -> str:
    """Return 'QUEUED' or 'EVAL_REJECTED'."""
    return evaluate_with_reason(job, filters=filters)[0]


def evaluate_with_reason(
    job: dict, *, filters: dict | None = None
) -> tuple[str, str | None]:
    """Return (status, reason). reason is None when status is 'QUEUED'.

    Gates applied in order (cheapest first):
      1. ACTIVE    — listing marked inactive → 'inactive'
      2. RECENCY   — date_posted older than recency_days → 'too-old'
      3. BLACKLIST — any blacklist term in title → 'blacklist'
      4. WHITELIST — no whitelist term in title → 'no-whitelist'

    Accepts both feed dicts (active: bool, date_posted: int unix ts) and
    DB row dicts (is_active: int 0/1, date_posted: ISO str).
    """
    _f = filters if filters is not None else _default_filters

    # Gate 1: active status.
    # Prefer 'is_active' (DB int), fall back to 'active' (feed bool).
    if "is_active" in job:
        if not job["is_active"]:
            return "EVAL_REJECTED", "inactive"
    elif "active" in job:
        if not job["active"]:
            return "EVAL_REJECTED", "inactive"

    # Gate 2: recency.
    recency_days = int(_f.get("recency_days", 45))
    if recency_days > 0:
        dp_raw = job.get("date_posted")
        if dp_raw is not None:
            try:
                if isinstance(dp_raw, (int, float)):
                    date_posted = datetime.fromtimestamp(dp_raw, tz=timezone.utc)
                else:
                    date_posted = datetime.fromisoformat(str(dp_raw))
                    if date_posted.tzinfo is None:
                        date_posted = date_posted.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
                if date_posted < cutoff:
                    return "EVAL_REJECTED", "too-old"
            except (ValueError, OSError):
                pass  # unparseable timestamp — don't reject on recency

    # Resolve title from either feed dict ('title') or DB row ('job_title').
    title = (job.get("title") or job.get("job_title") or "").lower()

    # Gate 3: blacklist takes priority over whitelist.
    for term in _f.get("blacklist", []):
        if term.lower() in title:
            return "EVAL_REJECTED", "blacklist"

    # Gate 4: whitelist — must match at least one term.
    # Empty list = gate disabled; all titles pass.
    whitelist = _f.get("whitelist", [])
    if whitelist and not any(term.lower() in title for term in whitelist):
        return "EVAL_REJECTED", "no-whitelist"

    return "QUEUED", None
