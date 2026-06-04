from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import httpx

from ajap import db
from ajap.filter import evaluate

logger = logging.getLogger(__name__)

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)


def _job_hash(
    source_id: str | None, company_name: str, job_title: str, url: str
) -> str:
    """Stable hash keyed on the feed's own UUID when present, else company+title+url."""
    if source_id:
        return hashlib.sha256(source_id.encode()).hexdigest()
    return hashlib.sha256((company_name + job_title + url).encode()).hexdigest()


def run_ingest(db_path: str) -> dict[str, int]:
    db.init_db(db_path)

    try:
        resp = httpx.get(LISTINGS_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        listings: list[dict] = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch listings: %s", exc)
        return {
            "fetched": 0,
            "new": 0,
            "queued": 0,
            "rejected": 0,
            "updated": 0,
            "demoted": 0,
            "url_collisions": 0,
        }

    logger.info("Sample record: %s", listings[0] if listings else "(empty)")

    fetched = len(listings)
    new_count = 0
    queued_count = 0
    rejected_count = 0
    updated_count = 0
    demoted_count = 0
    url_collision_count = 0

    conn = db.get_conn(db_path)
    try:
        for listing in listings:
            source_id = (listing.get("id") or "").strip() or None
            company = (listing.get("company_name") or "").strip()
            title = (listing.get("title") or "").strip()
            url = (listing.get("url") or "").strip()

            if not (company and title and url):
                continue

            is_active = 1 if listing.get("active") else 0
            ts = listing.get("date_posted")
            date_posted = (
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
            )

            h = _job_hash(source_id, company, title, url)

            if db.exists(conn, h):
                # Re-poll: reconcile active status, demote if needed.
                demoted = db.reconcile_active(conn, h, is_active)
                if demoted:
                    demoted_count += 1
                else:
                    updated_count += 1
            else:
                # New listing: evaluate and insert.
                status = evaluate(listing)
                inserted = db.insert_job(
                    conn,
                    job_hash=h,
                    company_name=company,
                    job_title=title,
                    application_url=url,
                    source_id=source_id,
                    is_active=is_active,
                    date_posted=date_posted,
                    status=status,
                )
                if inserted:
                    new_count += 1
                    if status == "QUEUED":
                        queued_count += 1
                    else:
                        rejected_count += 1
                else:
                    # UNIQUE(application_url) collision — different hash, same URL.
                    url_collision_count += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {
        "fetched": fetched,
        "new": new_count,
        "queued": queued_count,
        "rejected": rejected_count,
        "updated": updated_count,
        "demoted": demoted_count,
        "url_collisions": url_collision_count,
    }
    logger.info("Ingest summary: %s", summary)
    return summary
