from __future__ import annotations

import argparse
import logging

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
        f"{s['duplicates']} duplicates"
    )


def _run_refilter(db_path: str) -> None:
    """Re-evaluate every QUEUED row; demote failures to EVAL_REJECTED."""
    logger.info("Starting refilter pass → %s", db_path)
    conn = db.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT job_hash, job_title FROM job_applications WHERE execution_status = 'QUEUED'"
        ).fetchall()

        before = len(rows)
        reasons: dict[str, int] = {"inactive": 0, "blacklist": 0, "no-whitelist": 0}
        rejected = 0

        for row in rows:
            # DB rows have no 'active' field — inactive gate is intentionally skipped here.
            status, reason = evaluate_with_reason({"job_title": row["job_title"]})
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
        f"  Rejected: {rejected:>5}  (inactive: {reasons['inactive']}, "
        f"blacklist: {reasons['blacklist']}, no-whitelist: {reasons['no-whitelist']})"
    )
    print(f"  After  : {after:>6} QUEUED")


def main() -> None:
    parser = argparse.ArgumentParser(description="AJAP job-search pipeline")
    parser.add_argument(
        "--refilter",
        action="store_true",
        help="Re-evaluate all QUEUED rows through the keyword filter and demote failures",
    )
    args = parser.parse_args()

    db_path = config.DB_PATH
    if args.refilter:
        _run_refilter(db_path)
    else:
        _run_ingest(db_path)


if __name__ == "__main__":
    main()
