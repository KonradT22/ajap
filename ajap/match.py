from __future__ import annotations

import json
import logging
import random
import time
from typing import NamedTuple

from ajap import config, db

logger = logging.getLogger(__name__)

VALID_PICKS = frozenset({"swe", "quant", "mle", "mlops", "data", "devops", "cloud-infra"})
VALID_CONFIDENCE = frozenset({"STRONG", "MEDIUM", "WEAK"})

_IN_COST_PER_TOKEN = 0.15 / 1_000_000
_OUT_COST_PER_TOKEN = 0.60 / 1_000_000
_MIN_INTERVAL_SEC: float = 60.0 / max(config.GEMINI_RPM, 1)
_BACKOFF_BASE_SEC = 10.0
_BACKOFF_CAP_SEC = 120.0
_MAX_RETRIES = 5
_last_call_ts: list[float] = [0.0]

MAX_DESC_CHARS = 5_000

_SYSTEM = """\
You are a résumé routing assistant. Given a job description and 7 résumé lane descriptors, \
pick the single best résumé lane and honestly rate how well the candidate fits this specific role.

=== 7 RÉSUMÉ LANES ===
swe         — Generic full-stack/product SWE (React, Node, Flask, Rails). Default when no specialized angle applies.
              NOT for: infrastructure-ops, model serving, trading systems, data pipelines.
quant       — Trading systems, backtesting, market-data pipelines, risk controls; FMA internship + derivatives research background.
              Use for quant-dev, trading-infra, fintech. NOT for general SWE with no finance angle.
mle         — BUILD the model: PyTorch, neural nets, embeddings, research/modeling-heavy ML roles.
              NOT for roles focused on serving/monitoring models (use mlops) or general SWE.
mlops       — SERVE/MONITOR the model: inference pipelines, model monitoring & fallback, LLM APIs, scheduling/alerting.
              NOT for model research (use mle) or general cloud infra.
data        — Data engineering: SQL, ADF, ETL/ELT, stored procedures, data warehousing, analytics infrastructure.
              NOT for software delivery or platform reliability.
devops      — SHIP software: Docker/Compose, systemd, cron, Nginx, CI/CD pipelines, Linux. SRE/platform/reliability.
              NOT for cloud admin, networking, or IAM-heavy roles (use cloud-infra).
cloud-infra — RUN the systems/network: Azure, ESXi, Entra/IAM, Meraki/networking, DNS, firewalls, OT/industrial.
              NOT for software delivery or SRE roles (use devops).

=== FIT CONFIDENCE RULES ===
STRONG requires ALL THREE: (1) role type matches the chosen lane; (2) the JD's stated must-haves are \
directly evidenced in the lane descriptor — not adjacent, not inferred; (3) no hard requirement is \
unmet (specific degree field, domain knowledge, clearance, required years, required stack the lane \
doesn't show). STRONG = "genuinely competitive applicant on paper." Expect STRONG to be a MINORITY.

MEDIUM = right lane / role type, but real gaps exist: a required domain or significant stack chunk \
the descriptor doesn't cover, experience the candidate can't claim, or the JD's core asks go beyond \
what the lane shows. MEDIUM should be the PLURALITY — most roles land here.

WEAK = eligible but clearly off-target: the lane covers a different domain, the role demands \
specialized knowledge (specific language, hardware, industry, field of study) the lane shows none of, \
or the role type is a poor fit. WEAK should be a real tail, not near-zero.

HARD RULES — any ONE of these forces the rating DOWN from STRONG:
• JD requires a specific degree field not covered by a CS/CE background (biology, EE, finance, etc.)
• JD requires an active clearance or specific citizenship/visa the candidate hasn't established
• JD's primary language/framework/stack is not in the lane descriptor (e.g. Java/C++ for swe, Spark for data, K8s for devops)
• JD demands domain knowledge the lane descriptor does not explicitly show (finance domain for swe, wet-lab for mle, etc.)
• "Required" years of experience that a new grad cannot meet (3+ years stated as required)

Score against the descriptor AS WRITTEN. Never assume unstated experience.

=== OUTPUT ===
Return ONLY a single JSON object, no markdown, no text outside the JSON:
{"resume_pick": "<lane>", "resume_why": "<one concrete line>", "fit_confidence": "<STRONG|MEDIUM|WEAK>", "fit_reason": "<one concrete line>"}\
"""

_USER = """\
Career track (soft hint only): {track}
Title: {title}
Company: {company}
{desc_block}\
"""


class MatchResult(NamedTuple):
    resume_pick: str | None
    resume_why: str | None
    fit_confidence: str | None
    fit_reason: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _gemini_generate(client, model: str, contents, cfg):
    elapsed = time.monotonic() - _last_call_ts[0]
    gap = _MIN_INTERVAL_SEC - elapsed
    if gap > 0:
        time.sleep(gap)

    backoff = _BACKOFF_BASE_SEC
    for attempt in range(_MAX_RETRIES + 1):
        _last_call_ts[0] = time.monotonic()
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=cfg
            )
        except Exception as exc:
            exc_str = str(exc)
            if ("429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str) and attempt < _MAX_RETRIES:
                jitter = random.uniform(-0.1, 0.1) * backoff
                wait = min(backoff + jitter, _BACKOFF_CAP_SEC)
                logger.warning(
                    "429 rate-limit (attempt %d/%d); sleeping %.1f s",
                    attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, _BACKOFF_CAP_SEC)
                continue
            raise


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        obj = json.loads(text)
    except Exception:
        return None
    pick = (obj.get("resume_pick") or "").strip().lower()
    confidence = (obj.get("fit_confidence") or "").strip().upper()
    if pick not in VALID_PICKS or confidence not in VALID_CONFIDENCE:
        return None
    return {
        "resume_pick": pick,
        "resume_why": (obj.get("resume_why") or "").strip() or None,
        "fit_confidence": confidence,
        "fit_reason": (obj.get("fit_reason") or "").strip() or None,
    }


def match_one(
    client,
    model: str,
    title: str,
    company: str,
    description: str | None,
    career_track: str | None,
) -> MatchResult:
    from google.genai import types

    desc_block = f"Description:\n{description[:MAX_DESC_CHARS]}" if description else ""
    user_msg = _USER.format(
        track=career_track or "unknown",
        title=title,
        company=company,
        desc_block=desc_block,
    )

    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=200,
        system_instruction=_SYSTEM,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    total_in = 0
    total_out = 0
    parsed: dict | None = None

    for attempt in range(2):
        resp = _gemini_generate(client, model, user_msg, cfg)
        um = resp.usage_metadata
        total_in += um.prompt_token_count or 0
        total_out += um.candidates_token_count or 0
        parsed = _parse_json(resp.text or "")
        if parsed:
            break
        logger.warning(
            "Non-conforming JSON (attempt %d) for '%s': %r",
            attempt + 1, title, (resp.text or "")[:200],
        )

    cost = total_in * _IN_COST_PER_TOKEN + total_out * _OUT_COST_PER_TOKEN
    if parsed:
        return MatchResult(
            parsed["resume_pick"], parsed["resume_why"],
            parsed["fit_confidence"], parsed["fit_reason"],
            total_in, total_out, cost,
        )
    return MatchResult(None, None, None, None, total_in, total_out, cost)


def run_match(db_path: str, limit: int | None = None) -> dict:
    db.migrate_db(db_path)

    from google import genai

    model = config.GEMINI_MODEL
    client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"timeout": 90_000})

    conn = db.get_conn(db_path)
    try:
        query = (
            "SELECT job_hash, company_name, job_title, description, career_track "
            "FROM job_applications "
            "WHERE execution_status = 'PENDING_EXECUTION' AND resume_pick IS NULL"
        )
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    total = len(rows)
    confidence_counts: dict[str, int] = {"STRONG": 0, "MEDIUM": 0, "WEAK": 0}
    pick_counts: dict[str, int] = {}
    failed = 0
    total_in = 0
    total_out = 0
    total_cost = 0.0

    conn = db.get_conn(db_path)
    try:
        for i, row in enumerate(rows):
            result = match_one(
                client, model,
                row["job_title"], row["company_name"],
                row["description"], row["career_track"],
            )

            total_in += result.input_tokens
            total_out += result.output_tokens
            total_cost += result.cost_usd

            if result.resume_pick is None:
                failed += 1
                logger.error(
                    "Match failed after retry: '%s' (%s)",
                    row["job_title"], row["job_hash"],
                )
            else:
                db.update_match(
                    conn, row["job_hash"],
                    result.resume_pick, result.resume_why,
                    result.fit_confidence, result.fit_reason,
                )
                confidence_counts[result.fit_confidence] += 1
                pick_counts[result.resume_pick] = pick_counts.get(result.resume_pick, 0) + 1

            conn.commit()

            if i and i % 50 == 0:
                logger.info("Match: %d/%d done, cost so far $%.4f", i, total, total_cost)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "total": total,
        "confidence_counts": confidence_counts,
        "pick_counts": pick_counts,
        "failed": failed,
        "total_cost_usd": total_cost,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
    }
