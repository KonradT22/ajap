from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import gspread

from ajap import config, db

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

COLUMNS = ["Company", "Title", "Track", "Type", "Source", "Posted", "Location", "Status", "URL"]


# ── Formatting helpers (shared with dashboard.py) ─────────────────────────────


def _parse_location(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        locs = json.loads(raw)
        if isinstance(locs, list) and locs:
            return ", ".join(locs[:2]) + ("…" if len(locs) > 2 else "")
    except Exception:
        pass
    return raw or ""


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
        return str(val)[:10] if val else ""


def _hyperlink(url: str, label: str) -> str:
    safe_label = label.replace('"', "'")
    return f'=HYPERLINK("{url}","{safe_label}")'


def _row_to_cells(row) -> list:
    url = row["application_url"] or ""
    title = row["job_title"] or ""
    return [
        row["company_name"] or "",
        _hyperlink(url, title) if url else title,
        row["career_track"] or "",
        "Internship" if row["role_type"] == "internship" else "New Grad",
        row["source_name"] or "",
        _fmt_date(row["date_posted"]),
        _parse_location(row["locations_raw"]),
        row["application_status"] or "NEW",
        url,
    ]


# ── Sheets client ─────────────────────────────────────────────────────────────


def _get_client() -> gspread.Client:
    creds_path = config.GOOGLE_CREDENTIALS_PATH
    if not Path(creds_path).exists():
        raise FileNotFoundError(
            f"Service account credentials not found at {creds_path!r}.\n"
            "  1. Create a service account in Google Cloud Console.\n"
            "  2. Download the JSON key and save it at that path.\n"
            "  3. Share your Google Sheet with the service account's email."
        )
    return gspread.service_account(filename=creds_path, scopes=_SCOPES)


def _ensure_worksheet(sh: gspread.Spreadsheet, title: str, rows: int = 3000) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=len(COLUMNS))


def _write_tab(ws: gspread.Worksheet, data_rows: list[list]) -> int:
    ws.clear()
    all_rows = [COLUMNS] + data_rows
    ws.update(all_rows, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    return len(data_rows)


# ── Public entry point ────────────────────────────────────────────────────────


def _extract_sheet_id(value: str) -> str:
    """Accept either a bare ID or a full sheets URL."""
    if "/spreadsheets/d/" in value:
        part = value.split("/spreadsheets/d/")[1]
        return part.split("/")[0].split("?")[0]
    return value.strip()


def run_sync(db_path: str) -> dict[str, int]:
    if not config.GOOGLE_SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID is not set in .env")

    conn = db.get_conn(db_path)
    try:
        all_rows = conn.execute(
            "SELECT * FROM job_applications "
            "WHERE execution_status = 'PENDING_EXECUTION' "
            "ORDER BY date_posted DESC"
        ).fetchall()
        shortlist_rows = conn.execute(
            "SELECT * FROM job_applications "
            "WHERE execution_status = 'PENDING_EXECUTION' AND application_status = 'NEW' "
            "ORDER BY date_posted DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()

    gc = _get_client()
    sh = gc.open_by_key(_extract_sheet_id(config.GOOGLE_SHEET_ID))

    roles_ws = _ensure_worksheet(sh, "Roles", rows=max(len(all_rows) + 10, 100))
    roles_written = _write_tab(roles_ws, [_row_to_cells(r) for r in all_rows])
    logger.info("Synced %d rows to 'Roles' tab", roles_written)

    shortlist_ws = _ensure_worksheet(sh, "Shortlist", rows=30)
    shortlist_written = _write_tab(shortlist_ws, [_row_to_cells(r) for r in shortlist_rows])
    logger.info("Synced %d rows to 'Shortlist' tab", shortlist_written)

    return {"roles": roles_written, "shortlist": shortlist_written}
