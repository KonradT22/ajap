from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ajap import classify, config, db, enrich, ingest, match, notify

logger = logging.getLogger(__name__)


def pipeline_cycle(db_path: str) -> None:
    """One full incremental pipeline cycle. Safe to call concurrently — APScheduler
    is configured with max_instances=1, so it will never overlap."""
    t0 = time.monotonic()
    stamp = datetime.now().strftime("%H:%M:%S")
    logger.info("=== Cycle start %s ===", stamp)

    total_new = 0

    # ── 1. Simplify feeds ─────────────────────────────────────────────────────
    try:
        s = ingest.run_ingest(db_path)
        total_new += s["new"]
        logger.info(
            "Feeds: %d fetched / %d new (%d queued, %d rejected) / %d updated",
            s["fetched"], s["new"], s["queued"], s["rejected"], s["updated"],
        )
    except Exception as exc:
        logger.error("Feed ingest error: %s", exc)

    # ── 2. Board sources (inline JDs) ─────────────────────────────────────────
    try:
        s = ingest.run_ingest_boards(db_path)
        total_new += s["new"]
        logger.info(
            "Boards: %d fetched / %d new (%d queued, %d rejected) / %d updated",
            s["fetched"], s["new"], s["queued"], s["rejected"], s["updated"],
        )
    except Exception as exc:
        logger.error("Board ingest error: %s", exc)

    # ── 3. Enrich: JDs for QUEUED rows still missing descriptions ─────────────
    try:
        s = enrich.run_enrich(db_path)
        if s["total"]:
            logger.info(
                "Enrich: %d attempted / %d retrieved / %d failed",
                s["total"], s["retrieved"], s["failed"],
            )
    except Exception as exc:
        logger.error("Enrich error: %s", exc)

    # ── 4. Classify ──────────────────────────────────────────────────────────
    new_actionable: list[dict] = []
    try:
        s = classify.run_classify(db_path)
        if s["total"]:
            tc = s["track_counts"]
            logger.info(
                "Classify: %d rows → SWE=%d MLE=%d DE=%d IGNORE=%d failed=%d  $%.4f",
                s["total"],
                tc["GENERAL_SWE"], tc["MLOPS_MLE"], tc["DATA_ENGINEERING"],
                tc["IGNORE"], tc["failed"],
                s["total_cost_usd"],
            )
    except Exception as exc:
        logger.error("Classify error: %s", exc)

    # ── 5. Match: résumé routing for newly classified PENDING_EXECUTION rows ────
    try:
        s = match.run_match(db_path)
        if s["total"]:
            cc = s["confidence_counts"]
            logger.info(
                "Match: %d rows → STRONG=%d MEDIUM=%d WEAK=%d failed=%d  $%.4f",
                s["total"], cc["STRONG"], cc["MEDIUM"], cc["WEAK"],
                s["failed"], s["total_cost_usd"],
            )
    except Exception as exc:
        logger.error("Match error: %s", exc)

    # ── 6. Alert on unalerted PENDING_EXECUTION rows (persistent via alerted_at) ──
    conn = db.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT job_hash, company_name, job_title, career_track, application_url, "
            "resume_pick, fit_confidence "
            "FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' AND alerted_at IS NULL"
        ).fetchall()
        new_actionable = [
            {
                "hash": r["job_hash"],
                "company": r["company_name"],
                "title": r["job_title"],
                "track": r["career_track"] or "?",
                "url": r["application_url"],
                "resume_pick": r["resume_pick"],
                "fit_confidence": r["fit_confidence"],
            }
            for r in rows
        ]
    finally:
        conn.close()

    if new_actionable:
        sent = notify.send_alert(new_actionable)
        if sent:
            conn = db.get_conn(db_path)
            try:
                db.mark_alerted(conn, [m["hash"] for m in new_actionable])
                conn.commit()
            finally:
                conn.close()

    elapsed = time.monotonic() - t0
    logger.info(
        "=== Cycle done %s — %d new ingested / %d newly actionable / %.1fs ===",
        stamp, total_new, len(new_actionable), elapsed,
    )


def run_daemon(db_path: str) -> None:
    db.migrate_db(db_path)

    interval_min = config.POLL_INTERVAL_MINUTES
    jitter_sec = 300  # ±5 minutes

    logger.info(
        "Daemon starting — interval %d min ±%ds jitter, DB: %s",
        interval_min, jitter_sec, db_path,
    )

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        pipeline_cycle,
        IntervalTrigger(minutes=interval_min, jitter=jitter_sec, timezone="UTC"),
        args=[db_path],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        id="pipeline",
        name="AJAP pipeline cycle",
        next_run_time=datetime.now(timezone.utc),  # fire immediately on start
    )

    logger.info("First cycle firing immediately; next in ~%d min.", interval_min)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Daemon stopped.")
