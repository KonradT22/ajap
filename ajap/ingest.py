from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ajap import db
from ajap.filter import evaluate

logger = logging.getLogger(__name__)

SOURCES_PATH = Path("config/sources.json")

# Keep for backward-compat (used by main.py --refilter backfill).
LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)


# ── Source loader ──────────────────────────────────────────────────────────────


def load_sources() -> list[dict]:
    if not SOURCES_PATH.exists():
        logger.warning("No sources.json at %s — falling back to built-in new-grad URL", SOURCES_PATH)
        return [{"name": "simplify-newgrad", "type": "simplify_feed", "url": LISTINGS_URL}]
    with SOURCES_PATH.open() as fh:
        return json.load(fh)


# ── Fetchers ───────────────────────────────────────────────────────────────────


def _fetch_simplify_feed(url: str) -> list[dict]:
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


_FETCHERS = {
    "simplify_feed": _fetch_simplify_feed,
}


# ── Normalizer ─────────────────────────────────────────────────────────────────


def _normalize_simplify(listing: dict, source: dict) -> dict | None:
    """Convert a raw SimplifyJobs listing dict to our internal row shape.

    Returns None if required fields are missing or if the listing's terms
    don't intersect the source's allowed_terms filter.
    """
    allowed_terms: list[str] | None = source.get("allowed_terms")
    if allowed_terms is not None:
        listing_terms: list[str] = listing.get("terms") or []
        if not any(t in allowed_terms for t in listing_terms):
            return None

    source_id = (listing.get("id") or "").strip() or None
    company = (listing.get("company_name") or "").strip()
    title = (listing.get("title") or "").strip()
    url = (listing.get("url") or "").strip()

    if not (company and title and url):
        return None

    is_active = 1 if listing.get("active") else 0
    ts = listing.get("date_posted")
    date_posted = (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    )
    locations_raw = json.dumps(listing.get("locations") or [])

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "role_type": source.get("role_type", "new_grad"),
        "company_name": company,
        "job_title": title,
        "application_url": url,
        "is_active": is_active,
        "date_posted": date_posted,
        "locations_raw": locations_raw,
    }


_NORMALIZERS = {
    "simplify_feed": _normalize_simplify,
}


# ── Hash ───────────────────────────────────────────────────────────────────────


def _job_hash(source_id: str | None, company_name: str, job_title: str, url: str) -> str:
    """Stable hash keyed on the feed's own UUID when present, else company+title+url."""
    if source_id:
        return hashlib.sha256(source_id.encode()).hexdigest()
    return hashlib.sha256((company_name + job_title + url).encode()).hexdigest()


# ── Main ingest ────────────────────────────────────────────────────────────────


def run_ingest(db_path: str) -> dict[str, int]:
    db.migrate_db(db_path)

    sources = load_sources()

    totals: dict[str, int] = {
        "fetched": 0,
        "new": 0,
        "queued": 0,
        "rejected": 0,
        "updated": 0,
        "demoted": 0,
        "url_collisions": 0,
    }

    for source in sources:
        name = source["name"]
        feed_type = source["type"]
        url = source["url"]

        fetcher = _FETCHERS.get(feed_type)
        normalizer = _NORMALIZERS.get(feed_type)
        if not fetcher or not normalizer:
            logger.error("Unknown source type %r for source %r — skipping", feed_type, name)
            continue

        try:
            raw_listings = fetcher(url)
        except Exception as exc:
            logger.error("Failed to fetch source %r: %s", name, exc)
            continue

        logger.info("Source %r: fetched %d listings", name, len(raw_listings))
        totals["fetched"] += len(raw_listings)

        conn = db.get_conn(db_path)
        try:
            for listing in raw_listings:
                row = normalizer(listing, source)
                if row is None:
                    continue

                h = _job_hash(row["source_id"], row["company_name"], row["job_title"], row["application_url"])

                if db.exists(conn, h):
                    demoted = db.reconcile_active(conn, h, row["is_active"])
                    if demoted:
                        totals["demoted"] += 1
                    else:
                        totals["updated"] += 1
                else:
                    status = evaluate(row)
                    inserted = db.insert_job(
                        conn,
                        job_hash=h,
                        company_name=row["company_name"],
                        job_title=row["job_title"],
                        application_url=row["application_url"],
                        source_id=row["source_id"],
                        source_name=row["source_name"],
                        role_type=row["role_type"],
                        is_active=row["is_active"],
                        date_posted=row["date_posted"],
                        locations_raw=row["locations_raw"],
                        status=status,
                    )
                    if inserted:
                        totals["new"] += 1
                        if status == "QUEUED":
                            totals["queued"] += 1
                        else:
                            totals["rejected"] += 1
                    else:
                        totals["url_collisions"] += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    logger.info("Ingest summary (all sources): %s", totals)
    return totals
