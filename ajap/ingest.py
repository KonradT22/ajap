from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ajap import db
from ajap.filter import _is_non_us, evaluate

logger = logging.getLogger(__name__)

SOURCES_PATH = Path("config/sources.json")

# Keep for backward-compat (used by main.py --refilter backfill).
LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    stripped = _TAG_RE.sub(" ", _html.unescape(text))
    return re.sub(r"\s+", " ", stripped).strip() or None


# ── Board pre-filter vocabulary ────────────────────────────────────────────────

_ENG_DEPT_KEYWORDS: frozenset[str] = frozenset({
    "engineer", "software", "data", "ml", "machine learning", "ai",
    "platform", "infrastructure", "devops", "sre", "reliability",
    "security", "research", "scientist", "analytics", "backend",
    "frontend", "fullstack", "full-stack", "mobile", "cloud",
    "systems", "technical", "architecture", "database",
})

_NON_ENG_DEPT_KEYWORDS: frozenset[str] = frozenset({
    "sales", "marketing", "finance", "legal", "hr", "human resources",
    "recruiting", "talent", "operations", "customer success",
    "customer support", "customer service", "design", "product",
    "communications", "public relations", "business development",
    "account management", "partnerships", "administrative", "facilities",
})

# Word-level seniority check (split on whitespace/hyphens/slashes).
_SENIORITY_DROPS: frozenset[str] = frozenset({
    "senior", "staff", "principal", "lead", "director", "manager",
})


# ── Board pre-filter helpers ───────────────────────────────────────────────────


def _board_location_pass(locations: list[str], is_remote: bool = False) -> bool:
    """True if the role is US-accessible. Reuses the existing _is_non_us marker list."""
    if is_remote:
        return True
    if not locations:
        return True  # unspecified → keep (high-recall)
    return any(not _is_non_us(loc) for loc in locations)


def _passes_dept_gate(dept_names: list[str], title: str) -> bool:
    """Keep eng/data/ML; drop known non-eng depts; fall through to inclusive title check."""
    if dept_names:
        dept_text = " ".join(dept_names).lower()
        if any(kw in dept_text for kw in _NON_ENG_DEPT_KEYWORDS):
            return False  # confirmed non-eng
        if any(kw in dept_text for kw in _ENG_DEPT_KEYWORDS):
            return True   # confirmed eng
        # dept exists but is ambiguous — fall through to title
    # Unknown/ambiguous dept: inclusive title keyword check
    return any(kw in title.lower() for kw in _ENG_DEPT_KEYWORDS)


def _passes_seniority_gate(title: str) -> bool:
    """Drop titles containing seniority keywords at word boundaries."""
    words = set(re.split(r"[\s\-/,]+", title.lower()))
    return not bool(words & _SENIORITY_DROPS)


# ── Source loader ──────────────────────────────────────────────────────────────


def load_sources() -> list[dict]:
    if not SOURCES_PATH.exists():
        logger.warning("No sources.json at %s — falling back to built-in new-grad URL", SOURCES_PATH)
        return [{"name": "simplify-newgrad", "type": "simplify_feed", "url": LISTINGS_URL}]
    with SOURCES_PATH.open() as fh:
        return json.load(fh)


# ── Fetchers ───────────────────────────────────────────────────────────────────


def _fetch_simplify_feed(source: dict) -> list[dict]:
    resp = httpx.get(source["url"], timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _fetch_greenhouse_boards(source: dict) -> list[dict]:
    tokens: list[str] = json.loads(Path(source["boards_file"]).read_text())
    results: list[list[dict]] = [None] * len(tokens)  # type: ignore[list-item]
    dead = 0

    def _fetch(idx_token: tuple[int, str]) -> tuple[int, list[dict], bool]:
        idx, token = idx_token
        try:
            r = httpx.get(
                f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
                timeout=15, headers={"User-Agent": _UA}, follow_redirects=True,
            )
            if r.status_code == 404:
                return idx, [], True
            r.raise_for_status()
            jobs = r.json().get("jobs") or []
            for job in jobs:
                job["_board_token"] = token
            return idx, jobs, False
        except Exception as exc:
            logger.debug("Greenhouse board %r failed: %s", token, exc)
            return idx, [], True

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_fetch, (i, t)): t for i, t in enumerate(tokens)}
        done = 0
        for fut in as_completed(futures):
            idx, jobs, is_dead = fut.result()
            results[idx] = jobs
            if is_dead:
                dead += 1
            done += 1
            if done % 100 == 0:
                logger.info("Greenhouse: %d/%d boards fetched (%d dead)", done, len(tokens), dead)

    flat = [job for batch in results if batch for job in batch]
    logger.info("Greenhouse: %d boards, %d jobs, %d dead", len(tokens), len(flat), dead)
    return flat


def _fetch_lever_boards(source: dict) -> list[dict]:
    slugs: list[str] = json.loads(Path(source["boards_file"]).read_text())
    results: list[list[dict]] = [None] * len(slugs)  # type: ignore[list-item]
    dead = 0

    def _fetch(idx_slug: tuple[int, str]) -> tuple[int, list[dict], bool]:
        idx, slug = idx_slug
        try:
            r = httpx.get(
                f"https://api.lever.co/v0/postings/{slug}?mode=json",
                timeout=15, headers={"User-Agent": _UA}, follow_redirects=True,
            )
            if r.status_code == 404:
                return idx, [], True
            r.raise_for_status()
            postings = r.json() if isinstance(r.json(), list) else []
            for p in postings:
                p["_board_slug"] = slug
            return idx, postings, False
        except Exception as exc:
            logger.debug("Lever board %r failed: %s", slug, exc)
            return idx, [], True

    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {pool.submit(_fetch, (i, s)): s for i, s in enumerate(slugs)}
        done = 0
        for fut in as_completed(futures):
            idx, postings, is_dead = fut.result()
            results[idx] = postings
            if is_dead:
                dead += 1
            done += 1
            if done % 50 == 0:
                logger.info("Lever: %d/%d boards fetched (%d dead)", done, len(slugs), dead)

    flat = [p for batch in results if batch for p in batch]
    logger.info("Lever: %d boards, %d jobs, %d dead", len(slugs), len(flat), dead)
    return flat


_FETCHERS: dict = {
    "simplify_feed":     _fetch_simplify_feed,
    "greenhouse_boards": _fetch_greenhouse_boards,
    "lever_boards":      _fetch_lever_boards,
}


# ── Normalizers ────────────────────────────────────────────────────────────────


def _normalize_simplify(listing: dict, source: dict) -> dict | None:
    """Normalize a SimplifyJobs listing. Returns None to skip."""
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
    date_posted = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
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


def _normalize_greenhouse(listing: dict, source: dict) -> dict | None:
    """Normalize a Greenhouse board API job. Applies location/dept/seniority pre-filter."""
    title = (listing.get("title") or "").strip()
    url = (listing.get("absolute_url") or "").strip()
    if not (title and url):
        return None

    # ── Location gate ──
    loc_names: list[str] = []
    loc = listing.get("location") or {}
    if loc.get("name"):
        loc_names.append(loc["name"])
    for office in (listing.get("offices") or []):
        if office.get("name"):
            loc_names.append(office["name"])
    if not _board_location_pass(loc_names):
        return None

    # ── Dept gate ──
    dept_names = [d.get("name", "") for d in (listing.get("departments") or []) if d.get("name")]
    if not _passes_dept_gate(dept_names, title):
        return None

    # ── Seniority gate ──
    if not _passes_seniority_gate(title):
        return None

    # Normalize URL: collapse job-boards. → boards. to dedup with SimplifyJobs rows.
    norm_url = url.replace("//job-boards.greenhouse.io/", "//boards.greenhouse.io/")

    token = listing.get("_board_token", "")
    source_id = str(listing["id"]) if listing.get("id") else None
    description = _strip_html(listing.get("content"))

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "role_type": source.get("role_type", "new_grad"),
        "company_name": token,
        "job_title": title,
        "application_url": norm_url,
        "is_active": 1,
        "date_posted": listing.get("updated_at"),
        "locations_raw": json.dumps(loc_names),
        "description": description,
        "description_source": "greenhouse-board-api" if description else None,
    }


def _normalize_lever(listing: dict, source: dict) -> dict | None:
    """Normalize a Lever posting. Applies location/dept/seniority pre-filter."""
    title = (listing.get("text") or "").strip()
    url = (listing.get("hostedUrl") or "").strip()
    if not (title and url):
        return None

    cats = listing.get("categories") or {}
    is_remote = listing.get("workplaceType") == "remote"

    # ── Location gate ──
    all_locs: list[str] = cats.get("allLocations") or []
    if not all_locs and cats.get("location"):
        all_locs = [cats["location"]]
    if not _board_location_pass(all_locs, is_remote=is_remote):
        return None

    # ── Dept gate ──
    dept_names = [n for n in [cats.get("team"), cats.get("department")] if n]
    if not _passes_dept_gate(dept_names, title):
        return None

    # ── Seniority gate ──
    if not _passes_seniority_gate(title):
        return None

    slug = listing.get("_board_slug", "")
    source_id = listing.get("id")
    description = (listing.get("descriptionPlain") or "").strip() or _strip_html(listing.get("description"))
    if not description:
        description = None

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "role_type": source.get("role_type", "new_grad"),
        "company_name": slug,
        "job_title": title,
        "application_url": url,
        "is_active": 1,
        "date_posted": listing.get("createdAt"),
        "locations_raw": json.dumps(all_locs),
        "description": description,
        "description_source": "lever-api" if description else None,
    }


_NORMALIZERS: dict = {
    "simplify_feed":     _normalize_simplify,
    "greenhouse_boards": _normalize_greenhouse,
    "lever_boards":      _normalize_lever,
}


# ── Hash ───────────────────────────────────────────────────────────────────────


def _job_hash(source_id: str | None, company_name: str, job_title: str, url: str) -> str:
    if source_id:
        return hashlib.sha256(source_id.encode()).hexdigest()
    return hashlib.sha256((company_name + job_title + url).encode()).hexdigest()


# ── Main ingest ────────────────────────────────────────────────────────────────


def run_ingest(db_path: str) -> dict[str, int]:
    db.migrate_db(db_path)

    sources = load_sources()
    # Board-polling sources run only on explicit --ingest-boards; skip here.
    feed_sources = [s for s in sources if s["type"] == "simplify_feed"]

    totals: dict[str, int] = {
        "fetched": 0, "new": 0, "queued": 0, "rejected": 0,
        "updated": 0, "demoted": 0, "url_collisions": 0,
    }

    for source in feed_sources:
        name = source["name"]
        fetcher = _FETCHERS[source["type"]]
        normalizer = _NORMALIZERS[source["type"]]

        try:
            raw_listings = fetcher(source)
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
                    totals["demoted" if demoted else "updated"] += 1
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
                        totals["queued" if status == "QUEUED" else "rejected"] += 1
                    else:
                        totals["url_collisions"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    logger.info("Ingest summary (feed sources): %s", totals)
    return totals


# ── Board ingest ──────────────────────────────────────────────────────────────


def run_ingest_boards(db_path: str) -> dict[str, int]:
    """Ingest Greenhouse + Lever board sources with inline descriptions."""
    db.migrate_db(db_path)

    sources = load_sources()
    board_sources = [s for s in sources if s["type"] in ("greenhouse_boards", "lever_boards")]

    totals: dict[str, int] = {
        "fetched": 0, "new": 0, "queued": 0, "rejected": 0,
        "updated": 0, "demoted": 0, "url_collisions": 0,
    }

    for source in board_sources:
        name = source["name"]
        fetcher = _FETCHERS[source["type"]]
        normalizer = _NORMALIZERS[source["type"]]

        logger.info("Board ingest: fetching source %r ...", name)
        try:
            raw_listings = fetcher(source)
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
                    totals["demoted" if demoted else "updated"] += 1
                else:
                    status = evaluate(row)
                    inserted = db.insert_job(
                        conn,
                        job_hash=h,
                        company_name=row["company_name"],
                        job_title=row["job_title"],
                        application_url=row["application_url"],
                        source_id=row.get("source_id"),
                        source_name=row["source_name"],
                        role_type=row["role_type"],
                        is_active=row["is_active"],
                        date_posted=row.get("date_posted"),
                        locations_raw=row.get("locations_raw"),
                        description=row.get("description"),
                        description_source=row.get("description_source"),
                        status=status,
                    )
                    if inserted:
                        totals["new"] += 1
                        totals["queued" if status == "QUEUED" else "rejected"] += 1
                    else:
                        totals["url_collisions"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    logger.info("Board ingest summary: %s", totals)
    return totals


# ── Board dry-run ──────────────────────────────────────────────────────────────


def run_dry_run_boards(db_path: str) -> dict:
    """Fetch Greenhouse + Lever boards, apply pre-filter, report net-new vs DB.

    Nothing is written to the DB.
    """
    db.migrate_db(db_path)

    # Load existing URLs (normalized: collapse job-boards. → boards. for GH).
    conn = db.get_conn(db_path)
    existing_urls: set[str] = {
        r[0].replace("//job-boards.greenhouse.io/", "//boards.greenhouse.io/")
        for r in conn.execute("SELECT application_url FROM job_applications")
    }
    conn.close()

    sources = load_sources()
    board_sources = [s for s in sources if s["type"] in ("greenhouse_boards", "lever_boards")]

    report: dict[str, dict] = {}

    for source in board_sources:
        name = source["name"]
        fetcher = _FETCHERS[source["type"]]
        normalizer = _NORMALIZERS[source["type"]]

        logger.info("Dry-run: fetching source %r ...", name)
        raw_listings = fetcher(source)

        raw_count = len(raw_listings)
        dropped_location = 0
        dropped_dept = 0
        dropped_seniority = 0
        passed_filter = 0
        net_new = 0

        for listing in raw_listings:
            # Run gates individually for reporting granularity.
            title = (listing.get("title") or listing.get("text") or "").strip()

            # Location check
            if source["type"] == "greenhouse_boards":
                loc_names = []
                loc = listing.get("location") or {}
                if loc.get("name"):
                    loc_names.append(loc["name"])
                for o in (listing.get("offices") or []):
                    if o.get("name"):
                        loc_names.append(o["name"])
                loc_ok = _board_location_pass(loc_names)
            else:
                cats = listing.get("categories") or {}
                is_remote = listing.get("workplaceType") == "remote"
                all_locs = cats.get("allLocations") or []
                if not all_locs and cats.get("location"):
                    all_locs = [cats["location"]]
                loc_ok = _board_location_pass(all_locs, is_remote=is_remote)

            if not loc_ok:
                dropped_location += 1
                continue

            # Dept check
            if source["type"] == "greenhouse_boards":
                depts = [d.get("name", "") for d in (listing.get("departments") or []) if d.get("name")]
            else:
                cats = listing.get("categories") or {}
                depts = [n for n in [cats.get("team"), cats.get("department")] if n]

            if not _passes_dept_gate(depts, title):
                dropped_dept += 1
                continue

            # Seniority check
            if not _passes_seniority_gate(title):
                dropped_seniority += 1
                continue

            passed_filter += 1

            # Net-new check against DB
            row = normalizer(listing, source)
            if row is not None:
                norm_url = row["application_url"].replace(
                    "//job-boards.greenhouse.io/", "//boards.greenhouse.io/"
                )
                if norm_url not in existing_urls:
                    net_new += 1

        report[name] = {
            "raw": raw_count,
            "dropped_location": dropped_location,
            "dropped_dept": dropped_dept,
            "dropped_seniority": dropped_seniority,
            "passed_filter": passed_filter,
            "net_new": net_new,
        }
        logger.info(
            "Dry-run %r: raw=%d loc_drop=%d dept_drop=%d sen_drop=%d passed=%d net_new=%d",
            name, raw_count, dropped_location, dropped_dept, dropped_seniority, passed_filter, net_new,
        )

    return report
