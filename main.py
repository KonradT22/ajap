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
    LLM fields (career_track, resume_path, classify_reason) are cleared on rows
    that remain QUEUED so the next classify pass starts fresh.
    """
    import httpx
    import json

    logger.info("Starting refilter pass → %s", db_path)
    db.migrate_db(db_path)

    # Step 0: backfill locations_raw for rows that didn't have it on ingest.
    logger.info("Backfilling locations_raw from live feed...")
    try:
        resp = httpx.get(ingest.LISTINGS_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        listings: list[dict] = resp.json()
        sid_map: dict[str, str] = {
            (listing.get("id") or "").strip(): json.dumps(listing.get("locations") or [])
            for listing in listings
            if (listing.get("id") or "").strip()
        }
        conn_bf = db.get_conn(db_path)
        try:
            rows_missing = conn_bf.execute(
                "SELECT job_hash, source_id FROM job_applications WHERE locations_raw IS NULL"
            ).fetchall()
            updated_locs = 0
            for row in rows_missing:
                sid = row["source_id"]
                if sid and sid in sid_map:
                    conn_bf.execute(
                        "UPDATE job_applications SET locations_raw = ? WHERE job_hash = ?",
                        (sid_map[sid], row["job_hash"]),
                    )
                    updated_locs += 1
            conn_bf.commit()
            logger.info("Backfilled locations_raw for %d rows", updated_locs)
        finally:
            conn_bf.close()
    except Exception as exc:
        logger.warning("Location backfill failed: %s — continuing without it", exc)

    conn = db.get_conn(db_path)
    try:
        queued_rows = conn.execute(
            "SELECT job_hash, job_title, is_active, date_posted, locations_raw "
            "FROM job_applications WHERE execution_status = 'QUEUED'"
        ).fetchall()
        rejected_rows = conn.execute(
            "SELECT job_hash, job_title, is_active, date_posted, locations_raw "
            "FROM job_applications WHERE execution_status = 'EVAL_REJECTED'"
        ).fetchall()

        before_queued = len(queued_rows)

        demotion_reasons: dict[str, int] = {
            "inactive": 0,
            "non-us": 0,
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
                    "locations_raw": row["locations_raw"],
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
                    "locations_raw": row["locations_raw"],
                }
            )
            if status == "QUEUED":
                db.update_status(conn, row["job_hash"], "QUEUED")
                promoted += 1

        # Clear LLM fields on QUEUED rows so the next classify pass runs fresh.
        conn.execute(
            "UPDATE job_applications "
            "SET career_track = NULL, resume_path = NULL, classify_reason = NULL "
            "WHERE execution_status = 'QUEUED'"
        )

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
        f"non-us: {demotion_reasons['non-us']}, "
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


def _run_classify(db_path: str, limit: int | None) -> None:
    from ajap import classify

    audit = limit is not None
    logger.info(
        "Starting classification pass → %s (limit=%s, audit=%s)",
        db_path,
        limit or "none",
        audit,
    )
    s = classify.run_classify(db_path, limit=limit)

    print("\nClassification complete:")
    print(f"  Attempted : {s['total']:>6}")
    print(f"  Tokens in : {s['total_input_tokens']:>6}")
    print(f"  Tokens out: {s['total_output_tokens']:>6}")
    print(f"  Total cost: ${s['total_cost_usd']:.4f}")
    print(f"  Avg/row   : ${s['avg_cost_usd']:.5f}")
    print()
    print("  Track distribution:")
    for track, count in s["track_counts"].items():
        if count:
            print(f"    {track:<22}: {count}")

    if s["audit_rows"]:
        print()
        print("  Per-row audit (title | track | reason | tokens | cost):")
        for r in s["audit_rows"]:
            track_str = r["track"] or "FAILED"
            reason_str = r["reason"] or ""
            print(f"    [{track_str:<17}] {r['company'][:20]:<20} | {r['title'][:45]}")
            if reason_str:
                print(f"      ↳ {reason_str}")
            print(
                f"      tokens: {r['in_tok']}+{r['out_tok']}  cost: ${r['cost_usd']:.5f}"
            )


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


def _run_daemon(db_path: str) -> None:
    from ajap import daemon

    daemon.run_daemon(db_path)


def _run_match(db_path: str, limit: int | None) -> None:
    from ajap import match

    logger.info("Starting match pass → %s (limit=%s)", db_path, limit or "none")
    s = match.run_match(db_path, limit=limit)

    print("\nMatch complete:")
    print(f"  Attempted : {s['total']:>6}")
    print(f"  Failed    : {s['failed']:>6}")
    print(f"  Total cost: ${s['total_cost_usd']:.4f}")
    print()
    print("  Fit confidence:")
    for level in ("STRONG", "MEDIUM", "WEAK"):
        n = s["confidence_counts"].get(level, 0)
        if n:
            print(f"    {level:<8}: {n}")
    print()
    print("  Résumé pick distribution:")
    for pick, n in sorted(s["pick_counts"].items(), key=lambda x: -x[1]):
        print(f"    {pick:<14}: {n}")


def _run_sync_sheet(db_path: str) -> None:
    from ajap import sheets

    logger.info("Starting sheet sync → %s", db_path)
    result = sheets.run_sync(db_path)
    print(f"\nSheet sync complete:")
    print(f"  Roles tab    : {result['roles']:>5} rows")
    print(f"  Shortlist tab: {result['shortlist']:>5} rows")


def _run_ingest_boards(db_path: str) -> None:
    logger.info("Starting board ingest pass → %s", db_path)
    s = ingest.run_ingest_boards(db_path)
    print(
        f"\nBoard ingest complete: {s['fetched']} fetched / "
        f"{s['new']} new ({s['queued']} queued, {s['rejected']} rejected) / "
        f"{s['updated']} updated / {s['demoted']} demoted / "
        f"{s['url_collisions']} url-collisions"
    )


def _run_dry_run_boards(db_path: str) -> None:
    logger.info("Starting board dry-run → %s", db_path)
    report = ingest.run_dry_run_boards(db_path)

    print("\nBoard dry-run complete (nothing written to DB):")
    grand_raw = grand_pass = grand_new = 0
    for name, s in report.items():
        print(f"\n  [{name}]")
        print(f"    Raw roles fetched       : {s['raw']:>6}")
        print(f"    Dropped: location       : {s['dropped_location']:>6}")
        print(f"    Dropped: dept/category  : {s['dropped_dept']:>6}")
        print(f"    Dropped: seniority      : {s['dropped_seniority']:>6}")
        passed = s['passed_filter']
        print(f"    Passed pre-filter       : {passed:>6}  ({100*passed/max(s['raw'],1):.0f}% of raw)")
        print(f"    Net-new vs DB           : {s['net_new']:>6}  ({100*s['net_new']/max(passed,1):.0f}% of passed)")
        grand_raw += s['raw']
        grand_pass += passed
        grand_new += s['net_new']

    print(f"\n  TOTAL across all board sources:")
    print(f"    Raw                     : {grand_raw:>6}")
    print(f"    Passed pre-filter       : {grand_pass:>6}  ({100*grand_pass/max(grand_raw,1):.0f}% of raw)")
    print(f"    Net-new vs DB           : {grand_new:>6}  ({100*grand_new/max(grand_pass,1):.0f}% of passed)")


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
        "--classify",
        action="store_true",
        help="Classify QUEUED rows via Gemini; routes to track and sets resume path",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run the full pipeline on a 30-min ±5-min jitter loop with Discord/email alerts",
    )
    parser.add_argument(
        "--sync-sheet",
        action="store_true",
        help="Mirror PENDING_EXECUTION roles to Google Sheets (one-way, full rewrite)",
    )
    parser.add_argument(
        "--ingest-boards",
        action="store_true",
        help="Ingest Greenhouse + Lever boards with inline descriptions",
    )
    parser.add_argument(
        "--match",
        action="store_true",
        help="Pick résumé lane + rate fit for all unmatched PENDING_EXECUTION roles",
    )
    parser.add_argument(
        "--dry-run-boards",
        action="store_true",
        help="Fetch Greenhouse + Lever boards, apply pre-filter, report net-new (no DB writes)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap rows processed; enables per-row audit output for --classify",
    )
    args = parser.parse_args()

    db_path = config.DB_PATH
    if args.daemon:
        _run_daemon(db_path)
    elif args.match:
        _run_match(db_path, args.limit)
    elif args.sync_sheet:
        _run_sync_sheet(db_path)
    elif args.rebuild:
        _run_rebuild(db_path)
    elif args.refilter:
        _run_refilter(db_path)
    elif args.enrich:
        _run_enrich(db_path, args.limit)
    elif args.classify:
        _run_classify(db_path, args.limit)
    elif args.ingest_boards:
        _run_ingest_boards(db_path)
    elif args.dry_run_boards:
        _run_dry_run_boards(db_path)
    else:
        _run_ingest(db_path)


if __name__ == "__main__":
    main()
