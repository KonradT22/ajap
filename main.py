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
    """Re-evaluate QUEUED and EVAL_REJECTED rows through all current filter gates.

    QUEUED rows that now fail are demoted to EVAL_REJECTED.
    EVAL_REJECTED rows that now pass are promoted back to QUEUED.
    """
    logger.info("Starting refilter pass → %s", db_path)
    conn = db.get_conn(db_path)
    try:
        queued_rows = conn.execute(
            "SELECT job_hash, job_title, is_active, date_posted "
            "FROM job_applications WHERE execution_status = 'QUEUED'"
        ).fetchall()
        rejected_rows = conn.execute(
            "SELECT job_hash, job_title, is_active, date_posted "
            "FROM job_applications WHERE execution_status = 'EVAL_REJECTED'"
        ).fetchall()

        before_queued = len(queued_rows)

        demotion_reasons: dict[str, int] = {
            "inactive": 0,
            "too-old": 0,
            "blacklist": 0,
            "no-whitelist": 0,
        }
        demoted = 0
        promoted = 0

        for row in queued_rows:
            status, reason = evaluate_with_reason(
                {
                    "job_title": row["job_title"],
                    "is_active": row["is_active"],
                    "date_posted": row["date_posted"],
                }
            )
            if status == "EVAL_REJECTED":
                db.update_status(conn, row["job_hash"], "EVAL_REJECTED")
                demotion_reasons[reason] += 1
                demoted += 1

        for row in rejected_rows:
            status, _ = evaluate_with_reason(
                {
                    "job_title": row["job_title"],
                    "is_active": row["is_active"],
                    "date_posted": row["date_posted"],
                }
            )
            if status == "QUEUED":
                db.update_status(conn, row["job_hash"], "QUEUED")
                promoted += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    after_queued = before_queued - demoted + promoted
    print("\nRefilter complete:")
    print(f"  QUEUED before : {before_queued:>6}")
    print(
        f"  Demoted       : {demoted:>6}  "
        f"(inactive: {demotion_reasons['inactive']}, "
        f"too-old: {demotion_reasons['too-old']}, "
        f"blacklist: {demotion_reasons['blacklist']}, "
        f"no-whitelist: {demotion_reasons['no-whitelist']})"
    )
    print(f"  Promoted      : {promoted:>6}  (from EVAL_REJECTED → QUEUED)")
    print(f"  QUEUED after  : {after_queued:>6}")


def _run_rebuild(db_path: str) -> None:
    """Wipe the DB file and re-ingest from scratch."""
    p = Path(db_path)
    if p.exists():
        p.unlink()
        logger.info("Wiped %s", db_path)
    _run_ingest(db_path)


def _run_enrich(db_path: str, limit: int | None) -> None:
    from ajap import enrich

    logger.info("Starting enrichment pass → %s (limit=%s)", db_path, limit or "none")
    summary = enrich.run_enrich(db_path, limit=limit)
    print("\nEnrichment complete:")
    print(f"  Attempted : {summary['total']:>5}")
    print(f"  Retrieved : {summary['retrieved']:>5}")
    print(f"  Failed    : {summary['failed']:>5}")
    print()
    print("  By ATS (retrieved / failed):")
    for ats, count in summary["by_ats"].items():
        fail = summary["failures_by_ats"].get(ats, 0)
        if count or fail:
            print(f"    {ats:<22}: {count} ok / {fail} failed")
    if summary["samples"]:
        print()
        print("  Sample descriptions:")
        for url, ats, text in summary["samples"]:
            preview = text[:300].replace("\n", " ")
            print(f"    [{ats}] {url[:60]}")
            print(f"    {preview}...")
            print()


def main() -> None:
    parser = argparse.ArgumentParser(description="AJAP job-search pipeline")
    parser.add_argument(
        "--refilter",
        action="store_true",
        help="Re-evaluate QUEUED and EVAL_REJECTED rows; demote/promote as needed",
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="Wipe the DB and re-ingest from scratch"
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Fetch job descriptions for QUEUED rows missing them",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap rows processed (for --enrich testing)",
    )
    args = parser.parse_args()

    db_path = config.DB_PATH
    if args.rebuild:
        _run_rebuild(db_path)
    elif args.refilter:
        _run_refilter(db_path)
    elif args.enrich:
        _run_enrich(db_path, args.limit)
    else:
        _run_ingest(db_path)


if __name__ == "__main__":
    main()
