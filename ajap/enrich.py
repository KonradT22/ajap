from __future__ import annotations

import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import httpx

from ajap import db

logger = logging.getLogger(__name__)

RATE_LIMIT_SEC = 0.65  # ~1.5 req/sec

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
}


# ── HTML text extractor ───────────────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    _SKIP = frozenset(
        {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg"}
    )
    _BLOCK = frozenset(
        {
            "p",
            "div",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "br",
            "tr",
            "section",
            "article",
            "main",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0  # nesting depth inside a SKIP tag
        self._parts: list[str] = []
        self._block_break = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._depth += 1
        elif self._depth == 0 and tag in self._BLOCK:
            self._block_break = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._depth > 0:
            self._depth -= 1
        elif self._depth == 0 and tag in self._BLOCK:
            self._block_break = True

    def handle_data(self, data: str) -> None:
        if self._depth:
            return
        text = data.strip()
        if not text:
            return
        if self._block_break:
            self._parts.append("")
            self._block_break = False
        self._parts.append(text)

    def result(self) -> str:
        raw = "\n".join(self._parts)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _clean_html(html: str) -> str:
    ex = _TextExtractor()
    ex.feed(html)
    return ex.result()


# ── ATS detection ─────────────────────────────────────────────────────────────


def detect_ats(url: str) -> tuple[str, dict]:
    """Return (ats_type, params) for the URL.

    ats_type values:
      'greenhouse' | 'greenhouse_embed' | 'workday' | 'lever' | 'ashby' | 'other'
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path
    qs = parse_qs(parsed.query)
    parts = [p for p in path.split("/") if p]

    # Greenhouse direct board URLs (US + EU variants):
    #   job-boards.greenhouse.io/BOARD/jobs/JOB_ID
    #   job-boards.eu.greenhouse.io/BOARD/jobs/JOB_ID
    #   boards.greenhouse.io/BOARD/jobs/JOB_ID
    if (
        host
        in (
            "job-boards.greenhouse.io",
            "job-boards.eu.greenhouse.io",
        )
        and len(parts) >= 3
        and parts[1] == "jobs"
    ):
        return "greenhouse", {"board": parts[0], "job_id": parts[2]}

    if (
        host == "boards.greenhouse.io"
        and "embed" not in path
        and len(parts) >= 3
        and parts[1] == "jobs"
    ):
        return "greenhouse", {"board": parts[0], "job_id": parts[2]}

    # Greenhouse embed: boards.greenhouse.io/embed/job_app?token=TOKEN
    if host == "boards.greenhouse.io" and "embed" in path:
        token = (qs.get("token") or [None])[0]
        if token:
            return "greenhouse_embed", {"token": token}

    # Company page with ?gh_jid=JOB_ID — board resolved at fetch time via embed page
    gh_jid = (qs.get("gh_jid") or [None])[0]
    if gh_jid:
        return "greenhouse_embed", {"token": gh_jid}

    # Workday: {tenant}.wd*.myworkdayjobs.com/{careerSite}/job/{location}/{job-id}
    if "myworkdayjobs.com" in host:
        return "workday", {}

    # Lever: jobs.lever.co/COMPANY/UUID
    if host == "jobs.lever.co" and len(parts) >= 2:
        return "lever", {"company": parts[0], "job_id": parts[1]}

    # Ashby: jobs.ashbyhq.com/COMPANY/UUID[/application]
    if "ashbyhq.com" in host:
        job_parts = [p for p in parts if p != "application"]
        if len(job_parts) >= 2:
            return "ashby", {"company": job_parts[0], "job_id": job_parts[1]}

    return "other", {}


# ── per-ATS fetchers ──────────────────────────────────────────────────────────


def _fetch_greenhouse_api(client: httpx.Client, board: str, job_id: str) -> str | None:
    import html as _html

    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
    try:
        r = client.get(api_url)
        r.raise_for_status()
        # Greenhouse API HTML-escapes its content field (&lt;p&gt; etc.)
        raw = _html.unescape(r.json().get("content") or "")
        return _clean_html(raw) or None
    except Exception as exc:
        logger.debug("Greenhouse API failed %s/%s: %s", board, job_id, exc)
        return None


def _fetch_greenhouse_embed(client: httpx.Client, token: str) -> str | None:
    """Resolve board from embed page canonical URL, then call Greenhouse API."""
    try:
        r = client.get(f"https://boards.greenhouse.io/embed/job_app?token={token}")
        if r.status_code != 200:
            return None
        # Canonical href contains for=BOARD
        m = re.search(r'<link rel="canonical" href="[^"]*[?&]for=([^&"]+)', r.text)
        if not m:
            return None
        board = m.group(1)
        return _fetch_greenhouse_api(client, board, token)
    except Exception as exc:
        logger.debug("Greenhouse embed failed token=%s: %s", token, exc)
        return None


def _fetch_workday_cxs(client: httpx.Client, url: str) -> str | None:
    """Hit the Workday CXS JSON API derived from the public job page URL."""
    import html as _html

    parsed = urlparse(url)
    host = parsed.hostname or ""
    tenant = host.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]
    # Expect: [{careerSite}, "job", {location}, {job-id}]
    if len(parts) < 3 or parts[1] != "job":
        return None
    career_site = parts[0]
    job_path = "/".join(parts[2:])
    cxs_url = f"https://{host}/wday/cxs/{tenant}/{career_site}/job/{job_path}"
    try:
        r = client.get(cxs_url, headers={"Accept": "application/json"})
        r.raise_for_status()
        desc_html = r.json().get("jobPostingInfo", {}).get("jobDescription", "")
        return _clean_html(_html.unescape(desc_html)) or None
    except Exception as exc:
        logger.debug("Workday CXS failed %s: %s", url, exc)
        return None


def _fetch_lever_api(client: httpx.Client, company: str, job_id: str) -> str | None:
    api_url = f"https://api.lever.co/v0/postings/{company}/{job_id}"
    try:
        r = client.get(api_url)
        r.raise_for_status()
        data = r.json()
        sections: list[str] = []
        plain = data.get("descriptionPlain") or _clean_html(data.get("description", ""))
        if plain:
            sections.append(plain)
        for lst in data.get("lists", []):
            header = lst.get("text", "")
            body = _clean_html(lst.get("content", ""))
            if header:
                sections.append(header)
            if body:
                sections.append(body)
        extra = data.get("additionalPlain") or _clean_html(data.get("additional", ""))
        if extra:
            sections.append(extra)
        return "\n\n".join(sections) or None
    except Exception as exc:
        logger.debug("Lever API failed %s/%s: %s", company, job_id, exc)
        return None


def _fetch_ashby_html(client: httpx.Client, company: str, job_id: str) -> str | None:
    """Ashby job pages are SSR and embed the description in JSON-LD — no auth needed."""
    import json as _json
    import re as _re

    page_url = f"https://jobs.ashbyhq.com/{company}/{job_id}"
    try:
        r = client.get(page_url, follow_redirects=True)
        r.raise_for_status()
        matches = _re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', r.text, _re.S
        )
        for raw in matches:
            try:
                data = _json.loads(raw)
                html = data.get("description") or ""
                if html:
                    return _clean_html(html) or None
            except _json.JSONDecodeError:
                continue
        return _fetch_html_fallback(client, page_url)
    except Exception as exc:
        logger.debug("Ashby HTML failed %s/%s: %s", company, job_id, exc)
        return None


def _fetch_html_fallback(client: httpx.Client, url: str) -> str | None:
    """Generic HTTP GET + text extraction. Works for server-rendered pages."""
    try:
        r = client.get(url, follow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "html" not in ct:
            return None
        text = _clean_html(r.text)
        # Eightfold and similar platforms inline a JSON theme blob in non-script tags
        if '"themeOptions"' in text:
            return None
        # Heuristic: fewer than 200 chars → page is probably JS-only
        return text if len(text) >= 200 else None
    except Exception as exc:
        logger.debug("HTML fallback failed for %s: %s", url, exc)
        return None


def fetch_description(client: httpx.Client, url: str) -> tuple[str | None, str]:
    """Return (description_text | None, ats_type)."""
    ats, params = detect_ats(url)

    if ats == "greenhouse" and params:
        text = _fetch_greenhouse_api(client, params["board"], params["job_id"])
    elif ats == "greenhouse_embed" and params:
        text = _fetch_greenhouse_embed(client, params["token"])
    elif ats == "workday":
        text = _fetch_workday_cxs(client, url)
    elif ats == "lever" and params:
        text = _fetch_lever_api(client, params["company"], params["job_id"])
    elif ats == "ashby" and params:
        text = _fetch_ashby_html(client, params["company"], params["job_id"])
    else:
        text = _fetch_html_fallback(client, url)

    return text, ats


# ── main enrichment loop ──────────────────────────────────────────────────────

# Signatures that indicate a form-shell was captured rather than the real JD
_JUNK_SIGNATURES = (
    "Create a Job Alert",  # Greenhouse embed application form
    '"themeOptions"',  # Eightfold JSON blob (caught by fallback too; belt+suspenders)
)


def run_enrich(db_path: str, limit: int | None = None) -> dict:
    db.migrate_db(db_path)

    conn = db.get_conn(db_path)
    try:
        # Null out known-junk descriptions so the main loop re-fetches them
        for sig in _JUNK_SIGNATURES:
            conn.execute(
                "UPDATE job_applications SET description = NULL, description_source = NULL "
                "WHERE execution_status = 'QUEUED' AND description LIKE ?",
                (f"%{sig}%",),
            )
        conn.commit()

        query = (
            "SELECT job_hash, company_name, application_url "
            "FROM job_applications "
            "WHERE execution_status = 'QUEUED' AND description IS NULL"
        )
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    total = len(rows)
    ats_keys = ("greenhouse", "greenhouse_embed", "workday", "lever", "ashby", "other")
    counts: dict[str, int] = {k: 0 for k in ats_keys}
    failures: dict[str, int] = {k: 0 for k in ats_keys}
    samples: list[tuple[str, str, str]] = []

    conn = db.get_conn(db_path)
    try:
        with httpx.Client(
            timeout=20, headers=_HEADERS, follow_redirects=True
        ) as client:
            for i, row in enumerate(rows):
                if i and i % 25 == 0:
                    conn.commit()
                    logger.info("Enrichment: %d/%d done", i, total)

                desc, ats = fetch_description(client, row["application_url"])
                ats_key = ats if ats in counts else "other"

                db.update_description(
                    conn, row["job_hash"], desc, source=ats_key if desc else None
                )

                if desc:
                    counts[ats_key] += 1
                    if len(samples) < 3:
                        samples.append((row["application_url"], ats, desc))
                else:
                    failures[ats_key] += 1

                time.sleep(RATE_LIMIT_SEC)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "total": total,
        "retrieved": sum(counts.values()),
        "failed": sum(failures.values()),
        "by_ats": counts,
        "failures_by_ats": failures,
        "samples": samples,
    }
