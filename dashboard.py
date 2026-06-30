#!/usr/bin/env python3
"""Local dashboard for reviewing and tracking job applications."""
from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timezone
from threading import Timer

from flask import Flask, jsonify, render_template, request

from ajap import config, db

app = Flask(__name__)
DB_PATH = config.DB_PATH

_VALID_STATUSES = {"NEW", "INTERESTED", "APPLIED", "SKIPPED", "INTERVIEWING", "REJECTED", "OFFER"}
_VALID_SORTS = {"date_posted", "career_track", "company_name", "application_status", "fit_confidence"}


def _parse_location(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        locs = json.loads(raw)
        if isinstance(locs, list) and locs:
            shown = locs[:2]
            text = ", ".join(shown)
            return text + ("…" if len(locs) > 2 else "")
    except Exception:
        pass
    return raw


def _fmt_date(val: str | None) -> str:
    if not val:
        return ""
    try:
        ts = int(val)
        if ts > 1_000_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(val)[:10]


def _row_to_dict(row) -> dict:
    return {
        "job_hash": row["job_hash"],
        "company_name": row["company_name"],
        "job_title": row["job_title"],
        "application_url": row["application_url"],
        "career_track": row["career_track"] or "",
        "role_type": row["role_type"] or "new_grad",
        "source_name": row["source_name"] or "?",
        "date_posted": _fmt_date(row["date_posted"]),
        "location": _parse_location(row["locations_raw"]),
        "application_status": row["application_status"] or "NEW",
        "description": row["description"] or "",
        "resume_pick": row["resume_pick"] or "",
        "fit_confidence": row["fit_confidence"] or "",
        "fit_reason": row["fit_reason"] or "",
    }


@app.route("/")
def index():
    track_filter = request.args.get("track", "")
    role_type_filter = request.args.get("role_type", "")
    source_filter = request.args.get("source", "")
    status_filter = request.args.get("status", "")
    fit_filter = request.args.get("fit", "")
    resume_filter = request.args.get("resume", "")
    search = request.args.get("q", "").strip()
    sort_col = request.args.get("sort", "date_posted")
    sort_dir = request.args.get("dir", "desc")

    if sort_col not in _VALID_SORTS:
        sort_col = "date_posted"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    query = "SELECT * FROM job_applications WHERE execution_status = 'PENDING_EXECUTION'"
    params: list = []

    if track_filter:
        query += " AND career_track = ?"
        params.append(track_filter)
    if role_type_filter:
        query += " AND role_type = ?"
        params.append(role_type_filter)
    if source_filter:
        query += " AND source_name = ?"
        params.append(source_filter)
    if status_filter:
        query += " AND application_status = ?"
        params.append(status_filter)
    if fit_filter in ("STRONG", "MEDIUM", "WEAK"):
        query += " AND fit_confidence = ?"
        params.append(fit_filter)
    if resume_filter:
        query += " AND resume_pick = ?"
        params.append(resume_filter)
    if search:
        query += " AND (company_name LIKE ? OR job_title LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += f" ORDER BY {sort_col} {sort_dir.upper()}"

    conn = db.get_conn(DB_PATH)
    try:
        rows = conn.execute(query, params).fetchall()
        sources = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT source_name FROM job_applications "
                "WHERE execution_status='PENDING_EXECUTION' AND source_name IS NOT NULL "
                "ORDER BY source_name"
            ).fetchall()
        ]
        stats = dict(conn.execute(
            "SELECT application_status, COUNT(*) FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' GROUP BY application_status"
        ).fetchall())
        pick_stats = dict(conn.execute(
            "SELECT resume_pick, COUNT(*) FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' AND resume_pick IS NOT NULL "
            "GROUP BY resume_pick ORDER BY COUNT(*) DESC"
        ).fetchall())
    finally:
        conn.close()

    jobs = [_row_to_dict(r) for r in rows]
    return render_template(
        "index.html",
        jobs=jobs,
        total=len(jobs),
        sources=sources,
        stats=stats,
        track_filter=track_filter,
        role_type_filter=role_type_filter,
        source_filter=source_filter,
        status_filter=status_filter,
        fit_filter=fit_filter,
        resume_filter=resume_filter,
        pick_stats=pick_stats,
        search=search,
        sort_col=sort_col,
        sort_dir=sort_dir,
    )


@app.route("/shortlist")
def shortlist():
    conn = db.get_conn(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT * FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' AND application_status='NEW' "
            "AND fit_confidence='STRONG' "
            "ORDER BY date_posted DESC LIMIT 20"
        ).fetchall()
        strong_count = conn.execute(
            "SELECT COUNT(*) FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' AND application_status='NEW' "
            "AND fit_confidence='STRONG'"
        ).fetchone()[0]
        fit_stats = dict(conn.execute(
            "SELECT fit_confidence, COUNT(*) FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' AND fit_confidence IS NOT NULL "
            "GROUP BY fit_confidence"
        ).fetchall())
        pick_stats = dict(conn.execute(
            "SELECT resume_pick, COUNT(*) FROM job_applications "
            "WHERE execution_status='PENDING_EXECUTION' AND resume_pick IS NOT NULL "
            "GROUP BY resume_pick ORDER BY COUNT(*) DESC"
        ).fetchall())
    finally:
        conn.close()

    jobs = [_row_to_dict(r) for r in rows]
    return render_template(
        "shortlist.html",
        jobs=jobs,
        strong_count=strong_count,
        fit_stats=fit_stats,
        pick_stats=pick_stats,
    )


@app.route("/api/status", methods=["POST"])
def set_status():
    data = request.get_json(silent=True) or {}
    job_hash = data.get("job_hash", "")
    status = data.get("status", "")
    if not job_hash or status not in _VALID_STATUSES:
        return jsonify({"error": "invalid"}), 400
    conn = db.get_conn(DB_PATH)
    try:
        db.update_application_status(conn, job_hash, status)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "status": status})


def main():
    import sys
    no_browser = "--no-browser" in sys.argv
    db.migrate_db(DB_PATH)
    if not no_browser:
        Timer(0.8, lambda: webbrowser.open("http://localhost:5001")).start()
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
