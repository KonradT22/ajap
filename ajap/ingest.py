from __future__ import annotations

import hashlib
import logging

import httpx

from ajap import db
from ajap.filter import evaluate

logger = logging.getLogger(__name__)

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)


def _job_hash(company_name: str, job_title: str) -> str:
    return hashlib.sha256((company_name + job_title).encode()).hexdigest()


def run_ingest(db_path: str) -> dict[str, int]:
    db.init_db(db_path)

    try:
        resp = httpx.get(LISTINGS_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        listings: list[dict] = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch listings: %s", exc)
        return {"fetched": 0, "new": 0, "queued": 0, "rejected": 0, "duplicates": 0}

    logger.info("Sample record: %s", listings[0] if listings else "(empty)")

    fetched = len(listings)
    new_count = 0
    queued_count = 0
    rejected_count = 0
    dup_count = 0

    conn = db.get_conn(db_path)
    try:
        for listing in listings:
            company = (listing.get("company_name") or "").strip()
            title = (listing.get("title") or "").strip()
            url = (listing.get("url") or "").strip()

            if not (company and title and url):
                continue

            h = _job_hash(company, title)
            status = evaluate(listing)
            inserted = db.insert_job(
                conn,
                job_hash=h,
                company_name=company,
                job_title=title,
                application_url=url,
                status=status,
            )
            if inserted:
                new_count += 1
                if status == "QUEUED":
                    queued_count += 1
                else:
                    rejected_count += 1
            else:
                dup_count += 1

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
        "duplicates": dup_count,
    }
    logger.info("Ingest summary: %s", summary)
    return summary
