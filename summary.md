# AJAP — Development Summary

**Project:** Autonomous Job Application Pipeline, reframed as a **Job-Search Command Center**
**Repo:** `github.com/trest017-cmyk/ajap` (private)
**Built with:** Python + Claude Code, local-first, SQLite-backed
**Status:** Phases 1–3 complete; full classification run in progress.

---

## What this is (and what it deliberately isn't)

The original spec described a system that would auto-submit applications while evading
anti-bot detection (stealth browsers, fingerprint spoofing, CAPTCHA defeat). We scoped that
out by design. AJAP **discovers, filters, classifies, tracks, and (next) assists** — but the
final "Submit" stays with a human. No evasion, no auto-submit. That decision keeps the project
defensible (no ToS violations, no risk of getting flagged at firms you actually want) and
produces a higher-quality, smaller, accurate working set rather than spray-and-pray volume.

---

## Architecture

```
 Discover        Pre-filter        Enrich          Classify        (next) Review
 ────────   →   ───────────   →   ────────   →   ──────────   →   ─────────────
 poll feed      deterministic     fetch JDs       Gemini LLM       surface queue +
 (SimplifyJobs) gates (cheap)     via ATS APIs    route + fit      assisted fill
                                                  gate
        └──────────────────── SQLite (single source of state) ───────────────────┘
```

Design principle throughout: **cheap deterministic gates first, the LLM last.** Anything that
can be decided by a string match, a date, or a flag never costs an API call. The LLM is reserved
for the one judgment the cheap gates can't make — relevance/fit.

**Stack:** `httpx` (HTTP), stdlib `sqlite3` (state), `python-dotenv` (secrets),
`google-genai` (Gemini), `APScheduler` (scheduling, planned). Secrets in `.env`, identity in
`config/profile.json` — both gitignored.

---

## The data funnel

| Stage | Count |
|---|---|
| Total listings ingested (all-time archive) | 17,469 |
| Active (open) after the visibility gate | 2,014 |
| After recency (90 days) + blacklist | 1,343 |
| After US-only location gate → **classification pool** | **1,143** |
| Routed to a track vs. IGNORE | *(pending full run)* |

The active gate alone removes ~88% of the archive (closed/stale roles). The LLM then sorts the
survivors into tracks or rejects them as non-fits.

---

## Phases completed

### Phase 1 — Ingestion + storage
Polls the SimplifyJobs new-grad `listings.json`, deduplicates, and writes to SQLite. Dedup was
originally keyed on `SHA-256(company + title)` but that silently collapsed ~1,633 genuinely
distinct postings (same generic title, different roles/URLs), so it was re-keyed on the feed's
stable `source_id`. Result: 17,469 listings stored.

### Phase 2 — Deterministic pre-filter
Config-driven filtering in `config/filters.json`. Evolved significantly from the original spec:
- The mandatory **keyword whitelist was dropped** — applied to an already-curated new-grad feed
  it was double-filtering and killing ~92% of legitimate open roles. The blacklist
  (Senior/Staff/Principal/Lead) was kept as a cheap safety net.
- Added an **active/visibility gate** (the real workhorse).
- Added a **recency gate** (90 days; widened from 45 once we realized `is_active` already
  guarantees openness and a tight window was discarding live roles).
- Added a **US-only location gate** (high-recall: keeps a role if any location is US, remote, or
  unspecified; rejects only if all locations are clearly non-US).

### Phase 2.5 — Data-model consolidation
Done while the DB was still fully derived from the feed (and therefore disposable). Added
`source_id`, `is_active`, `date_posted`, and `description_source` columns, re-keyed dedup, and
switched ingest from insert-only to **upsert-and-reconcile** so roles that later close get
demoted instead of lingering as open forever.

### Phase 3a / 3a.2 — Job-description enrichment
The classifier needs the JD, but the feed only carries titles — so this phase retrieves
descriptions, ATS-aware:
- **Greenhouse, Lever, Ashby** via their public JSON APIs (94–100% hit rates).
- **Greenhouse embed** URLs resolved via the `<link rel="canonical">` board-token trick (they
  were returning the application form shell, not the JD — a quality leak that's now fixed).
- **Workday** — the big win. Workday was ~40% of the pool and looked "unreachable without a
  browser," but its sites are backed by a public `/wday/cxs/...` JSON API. A dedicated handler
  recovered **88%** of Workday roles with no headless browser, no evasion.
- Eightfold junk-blob guard.

Coverage went from **45% → 81%** (1,086 of 1,343 with real descriptions). The remaining ~19% is
a fragmented long tail (Workable/iCIMS/Taleo/SuccessFactors SPAs and dead 404 postings) not worth
bespoke handlers; the classifier falls back to title-only for those.

### Phase 3b — LLM classification (the brain)
Gemini does two jobs in one strict, single-token call: **route** into
`DATA_ENGINEERING` / `MLOPS_MLE` / `GENERAL_SWE`, or **IGNORE** as a candidate-fit gate using a
summary from `profile.json`.

Notable fixes and calibration:
- Migrated off `gemini-2.0-flash` (retired by Google March 2026) to `gemini-2.5-flash`, with
  `thinking_budget=0` — 2.5's default thinking was silently eating the output-token budget.
- Calibrated the IGNORE gate against a 25-row audit: removed a vibe-based "not a new-grad"
  catch-all and set seniority rejection to require a **concrete** signal — a stated 3+ year
  minimum, or a Senior/Staff/Principal/Lead/Director/Manager title. Modest "≤2 years" /
  "preferred" / "0–2/1–2/2–5" ranges no longer disqualify (per "cast wide").
- **Quant policy:** software/engineering roles at trading firms route to a track; pure quant
  *research*/trader/analyst roles are IGNOREd (your call).
- Persists `career_track`, `resume_path`, and `classify_reason`; audit mode logs reasons.
- Rate-limit throttle + 429 backoff for the free tier.

**Cost:** ~**$0.12** for the entire 1,143-row pool — against a $0.05-*per-application* budget,
effectively free.

---

## Database schema (`job_applications`)

```
job_hash              TEXT PRIMARY KEY      -- keyed on the feed's stable id
source_id             TEXT                  -- SimplifyJobs feed id
company_name          TEXT NOT NULL
job_title             TEXT NOT NULL
application_url       TEXT UNIQUE NOT NULL
career_track          TEXT                  -- DATA_ENGINEERING | MLOPS_MLE | GENERAL_SWE | IGNORE
resume_path           TEXT                  -- résumé chosen from profile.json
execution_status      TEXT NOT NULL         -- state machine (below)
classify_reason       TEXT                  -- LLM's one-line rationale (audit)
description           TEXT                  -- retrieved JD
description_source     TEXT                  -- greenhouse | workday | lever | ashby | ...
is_active             INTEGER               -- from the feed
date_posted           DATETIME
timestamp_discovered  DATETIME
timestamp_updated     DATETIME
```

**Status states:** `QUEUED → PENDING_EXECUTION` (routed, résumé attached) or `EVAL_REJECTED`
(filtered out or IGNOREd). Plus `PROCESSING`, `CAPTCHA_BLOCKED`, `SESSION_TIMEOUT`, `SUBMITTED`,
`FAILED` reserved for later phases.

---

## Key decisions & lessons

- **Human keeps the submit step** — the line that defines the whole project.
- **Cheap gates first, LLM last** — kept cost at pennies and the LLM focused on judgment.
- **Source-aware filtering** — don't double-filter a feed that's already curated.
- **Verify, don't trust** — e.g., pulling the ByteDance JD confirmed its PhD requirement was
  real, not a misclassification; repeated audits before scaling.
- **Public APIs over browser rendering** — the Workday CXS endpoint beat "needs Playwright."
- **Fix the schema while it's disposable** — consolidate before there's real state to lose.

---

## Roadmap (not yet built)

- **Phase 4 — Tracking + alerts:** surface the classified queue; Discord/email pings on strong
  matches; manage status as you go.
- **Phase 5 — Résumé tailoring:** suggest targeted edits per role.
- **Phase 6 — Assisted fill:** open a role's form, pre-populate from `profile.json`, hand you the
  review-and-submit. (No stealth, no auto-submit.)
- **Continuous operation:** the APScheduler polling loop with jitter; incremental enrichment of
  only new rows.
- **Optional:** Google Sheets mirror; splitting `MLOPS_MLE` into separate MLE / MLOps tracks.