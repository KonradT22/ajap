from __future__ import annotations

import logging
import random
import time
from typing import NamedTuple

from ajap import config, db

logger = logging.getLogger(__name__)

VALID_TRACKS = frozenset({"DATA_ENGINEERING", "MLOPS_MLE", "GENERAL_SWE", "IGNORE"})
MAX_DESC_CHARS = 6_000

# Gemini 2.5 Flash pricing (USD per token, ≤128 K context, non-thinking tier)
_IN_COST_PER_TOKEN = 0.15 / 1_000_000
_OUT_COST_PER_TOKEN = 0.60 / 1_000_000

# Rate-limit: free tier ≈ 10 RPM → enforce ≥ 6.1 s between calls.
_MIN_INTERVAL_SEC = 6.1
_BACKOFF_BASE_SEC = 10.0
_BACKOFF_CAP_SEC = 120.0
_last_call_ts: list[float] = [0.0]  # mutable singleton; list avoids nonlocal

# ── Prompt templates ──────────────────────────────────────────────────────────

_SYSTEM = """\
You are a job-application router for a new-grad software / data candidate.

=== CANDIDATE ===
{candidate_summary}

=== CATEGORIES ===
DATA_ENGINEERING  – Data pipelines, warehousing, ETL/ELT, analytics infrastructure,
                    data platform engineering, streaming systems, BI infrastructure.
MLOPS_MLE         – ML platform, model deployment, ML infrastructure, MLOps,
                    ML Engineering, LLM/AI infrastructure, model serving.
GENERAL_SWE       – All other backend, full-stack, systems, platform, distributed
                    systems, or general software engineering roles.
IGNORE            – Not a fit for this candidate.

=== IGNORE WHEN ANY OF THESE APPLY ===
• The JD states a HARD MINIMUM of 3 or more years of experience — meaning the
  listing uses language like "3+ years required", "minimum 3 years", "at least 3
  years", or similar. Do NOT IGNORE based on experience when:
    – the requirement is ≤ 2 years (e.g. "1-2 years", "2 years required");
    – experience is described as "preferred", "nice-to-have", or "a plus";
    – the range has a low end ≤ 2 (e.g. "0-2", "1-2", "2-5" years).
  Also IGNORE when the job title contains Senior / Staff / Principal / Lead /
  Director / Manager regardless of stated experience.
• Not a software, data, or ML role (operations, finance, sales, HR, legal,
  QA-only, support, design, product, project management, etc.).
  Exception: software/infrastructure/data-engineering roles at trading or quant firms
  are NOT ignored — route them to the best-fit track (usually GENERAL_SWE).
• Role is purely quant research, trading strategy, or portfolio management with no
  meaningful software-engineering component.
• Requires an active security clearance (TS, SCI, TS/SCI, etc.).
• Requires citizenship, visa status, or work-authorisation the candidate does not hold.
• Requires a PhD or post-doctoral qualification as a HARD prerequisite (the listing
  explicitly states PhD required or the role is a postdoctoral position).

Do NOT factor in job location when routing — the location filter runs separately.
Do NOT use general vibe or "seems mid-level" reasoning to IGNORE — only the
concrete signals above.

Route everything else into one of the three SWE/data buckets above.\
"""

_USER_FULL = """\
Title: {title}
Company: {company}
{desc_block}
Reply with EXACTLY ONE of: DATA_ENGINEERING  MLOPS_MLE  GENERAL_SWE  IGNORE
No other text.\
"""

_USER_AUDIT = """\
Title: {title}
Company: {company}
{desc_block}
Reply with EXACTLY ONE of: DATA_ENGINEERING  MLOPS_MLE  GENERAL_SWE  IGNORE
then a space and a reason of ≤ 12 words explaining the routing or IGNORE decision.
Example: GENERAL_SWE backend API role, no ML or data infra\
"""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_candidate_summary(profile: dict) -> str:
    edu = profile.get("education", {})
    demo = profile.get("demographics", {})
    return (
        f"Degree: {edu.get('degree', '?')} in {edu.get('major', '?')}, "
        f"graduating {edu.get('graduation_date', '?')}\n"
        f"Citizenship: {demo.get('citizenship', '?')}\n"
        f"Visa sponsorship required: {demo.get('visa_sponsorship_required', '?')}"
    )


def _parse_track(text: str) -> tuple[str | None, str | None]:
    """Extract (track, reason) from a model response line.

    First whitespace-delimited token must be a valid track. Everything after
    the first token is treated as the optional reason (audit mode).
    Returns (None, None) for any non-conforming output.
    """
    text = text.strip()
    parts = text.split(None, 1)
    if not parts:
        return None, None
    token = parts[0].rstrip(".,;:").upper()
    reason = parts[1].strip() if len(parts) > 1 else None
    if token not in VALID_TRACKS:
        return None, None
    return token, reason


# ── Rate-limited API wrapper ──────────────────────────────────────────────────


def _gemini_generate(client, model: str, contents, cfg):
    """Call Gemini with inter-call throttle and exponential back-off on 429."""
    elapsed = time.monotonic() - _last_call_ts[0]
    gap = _MIN_INTERVAL_SEC - elapsed
    if gap > 0:
        time.sleep(gap)

    backoff = _BACKOFF_BASE_SEC
    while True:
        _last_call_ts[0] = time.monotonic()
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=cfg
            )
        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str:
                jitter = random.uniform(-0.1, 0.1) * backoff
                wait = min(backoff + jitter, _BACKOFF_CAP_SEC)
                logger.warning("429 rate-limit; sleeping %.1f s", wait)
                time.sleep(wait)
                backoff = min(backoff * 2, _BACKOFF_CAP_SEC)
                continue
            raise


# ── Classification call ───────────────────────────────────────────────────────


class ClassifyResult(NamedTuple):
    track: str | None  # None = bad response even after retry
    reason: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float


def classify_one(
    client,
    model: str,
    title: str,
    company: str,
    description: str | None,
    candidate_summary: str,
    audit_mode: bool,
) -> ClassifyResult:
    from google.genai import types

    desc_block = ""
    if description:
        truncated = description[:MAX_DESC_CHARS]
        desc_block = f"Description:\n{truncated}"

    user_tmpl = _USER_AUDIT if audit_mode else _USER_FULL
    user_msg = user_tmpl.format(title=title, company=company, desc_block=desc_block)
    system = _SYSTEM.format(candidate_summary=candidate_summary)

    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=60 if audit_mode else 20,
        stop_sequences=["\n"],
        system_instruction=system,
        # gemini-2.5-flash thinks by default; thinking consumes max_output_tokens budget
        # and leaves text=None. Disable it — single-token routing doesn't need CoT.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    total_in = 0
    total_out = 0
    track: str | None = None
    reason: str | None = None

    for attempt in range(2):
        resp = _gemini_generate(client, model, user_msg, cfg)
        um = resp.usage_metadata
        total_in += um.prompt_token_count or 0
        total_out += um.candidates_token_count or 0
        track, reason = _parse_track(resp.text or "")
        if track:
            break
        logger.warning(
            "Non-conforming response (attempt %d) for '%s': %r",
            attempt + 1,
            title,
            resp.text,
        )

    cost = total_in * _IN_COST_PER_TOKEN + total_out * _OUT_COST_PER_TOKEN
    return ClassifyResult(track, reason, total_in, total_out, cost)


# ── Main classification loop ──────────────────────────────────────────────────


def run_classify(db_path: str, limit: int | None = None) -> dict:
    db.migrate_db(db_path)

    profile = config.load_profile()
    candidate_summary = _build_candidate_summary(profile)
    resume_mappings: dict[str, str] = profile.get("resume_mappings", {})

    from google import genai

    model = config.GEMINI_MODEL
    client = genai.Client(api_key=config.GEMINI_API_KEY)

    audit_mode = limit is not None

    conn = db.get_conn(db_path)
    try:
        query = (
            "SELECT job_hash, company_name, job_title, description "
            "FROM job_applications "
            "WHERE execution_status = 'QUEUED' AND career_track IS NULL"
        )
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    total = len(rows)
    track_counts: dict[str, int] = {
        "DATA_ENGINEERING": 0,
        "MLOPS_MLE": 0,
        "GENERAL_SWE": 0,
        "IGNORE": 0,
        "failed": 0,
    }
    total_in = 0
    total_out = 0
    total_cost = 0.0
    audit_rows: list[dict] = []

    conn = db.get_conn(db_path)
    try:
        for i, row in enumerate(rows):
            result = classify_one(
                client,
                model,
                row["job_title"],
                row["company_name"],
                row["description"],
                candidate_summary,
                audit_mode,
            )

            total_in += result.input_tokens
            total_out += result.output_tokens
            total_cost += result.cost_usd

            if result.track is None:
                logger.error(
                    "Classification failed after retry: '%s' (%s)",
                    row["job_title"],
                    row["job_hash"],
                )
                track_counts["failed"] += 1
            elif result.track == "IGNORE":
                db.update_classification(
                    conn, row["job_hash"], "IGNORE", None, "EVAL_REJECTED", result.reason
                )
                track_counts["IGNORE"] += 1
            else:
                resume_path = resume_mappings.get(result.track)
                db.update_classification(
                    conn,
                    row["job_hash"],
                    result.track,
                    resume_path,
                    "PENDING_EXECUTION",
                    result.reason,
                )
                track_counts[result.track] += 1

            conn.commit()

            if audit_mode:
                audit_rows.append(
                    {
                        "title": row["job_title"],
                        "company": row["company_name"],
                        "track": result.track,
                        "reason": result.reason,
                        "in_tok": result.input_tokens,
                        "out_tok": result.output_tokens,
                        "cost_usd": result.cost_usd,
                    }
                )

            if i and i % 50 == 0:
                logger.info(
                    "Classify: %d/%d done, cost so far $%.4f", i, total, total_cost
                )

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    avg_cost = total_cost / total if total else 0.0
    return {
        "total": total,
        "track_counts": track_counts,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": total_cost,
        "avg_cost_usd": avg_cost,
        "audit_rows": audit_rows,
    }
