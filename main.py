from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ajap import config, db, ingest
from ajap.filter import evaluate_with_reason

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ajap.main")


def _run_ingest(db_path: str) -> None:
    logger.info("Starting ingest pass → %s", db_path)
    s = ingest.run_ingest(db_path)
    print(
        f"\nIngest complete: {s['fetched']} fetched / "
        f"{s['new']} new ({s['queued']} queued, {s['rejected']} rejected) / "
        f"{s['updated']} updated / {s['demoted']} demoted / "
        f"{s['url_collisions']} url-collisions"
    )


def _run_refilter(db_path: str) -> None:
    """Re-evaluate every QUEUED row through all current filter gates."""
    logger.info("Starting refilter pass → %s", db_path)
    conn = db.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT job_hash, job_title, is_active, date_posted "
            "FROM job_applications WHERE execution_status = 'QUEUED'"
        ).fetchall()

        before = len(rows)
        reasons: dict[str, int] = {
            "inactive": 0,
            "too-old": 0,
            "blacklist": 0,
            "no-whitelist": 0,
        }
        rejected = 0

        for row in rows:
            status, reason = evaluate_with_reason(
                {
                    "job_title": row["job_title"],
                    "is_active": row["is_active"],
                    "date_posted": row["date_posted"],
                }
            )
            if status == "EVAL_REJECTED":
                db.update_status(conn, row["job_hash"], "EVAL_REJECTED")
                reasons[reason] += 1
                rejected += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    after = before - rejected
    print("\nRefilter complete:")
    print(f"  Before : {before:>6} QUEUED")
    print(
        f"  Rejected: {rejected:>5}  "
        f"(inactive: {reasons['inactive']}, "
        f"too-old: {reasons['too-old']}, "
        f"blacklist: {reasons['blacklist']}, "
        f"no-whitelist: {reasons['no-whitelist']})"
    )
    print(f"  After  : {after:>6} QUEUED")


def _run_rebuild(db_path: str) -> None:
    """Wipe the DB file and re-ingest from scratch."""
    p = Path(db_path)
    if p.exists():
        p.unlink()
        logger.info("Wiped %s", db_path)
    _run_ingest(db_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="AJAP job-search pipeline")
    parser.add_argument(
        "--refilter",
        action="store_true",
        help="Re-evaluate all QUEUED rows through the filter and demote failures",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Wipe the DB and re-ingest from scratch (schema v2 migration)",
    )
    args = parser.parse_args()

    db_path = config.DB_PATH
    if args.rebuild:
        _run_rebuild(db_path)
    elif args.refilter:
        _run_refilter(db_path)
    else:
        _run_ingest(db_path)


if __name__ == "__main__":
    main()
