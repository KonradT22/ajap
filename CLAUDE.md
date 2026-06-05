# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

AJAP is a local-first job-search pipeline that discovers, filters, classifies, and tracks new-grad software/data-infrastructure roles. It is an intelligence and tracking tool — **not** an auto-submitter. The final submit step stays with the user.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                       # fill in real keys
cp config/profile.example.json config/profile.json        # fill in your data
```

## Commands

```bash
# Lint
ruff check .
ruff format .

# Tests
pytest
pytest tests/test_foo.py::test_bar   # single test
```

## Architecture

Four-stage pipeline, all state in a single SQLite file (`data/ajap.db`):

1. **Ingest** (`ajap/ingest.py`) — polls SimplifyJobs repo and public Greenhouse/Lever/Ashby boards; deduplicates via `SHA-256(company+title)`; writes raw rows.
2. **Pre-filter** (`ajap/filter.py`) — regex whitelist/blacklist on title; marks non-matches `EVAL_REJECTED`.
3. **Classify** (`ajap/classify.py`) — routes survivors via Gemini 2.5 Flash into `DATA_ENGINEERING` / `MLOPS_MLE` / `GENERAL_SWE` / `IGNORE`; attaches matching résumé path from `profile.json`.
4. **Notify** (`ajap/notify.py`) — Discord webhook and/or email alerts on strong matches.

`main.py` is the entrypoint and APScheduler-based polling loop. `ajap/config.py` loads `.env` and `config/profile.json`. `ajap/db.py` owns the SQLite schema and all query helpers.

## Configuration files

- `.env` — secrets (Gemini API key, Discord webhook, optional Google Sheets creds). See `.env.example`.
- `config/profile.json` — personal data, location preferences, résumé-to-track mappings. See `config/profile.example.json`. Both are gitignored.
- `resumes/` and `data/` are gitignored (PII + generated state).

## LLM usage

Classification calls go to Gemini (`google-genai` SDK). Only public job-listing text is sent — no personal data. Model is configurable via `GEMINI_MODEL` in `.env` (default `gemini-2.5-flash`; 2.0-flash was retired March 2026).

## Roadmap phase status

The scaffold exists; all six phases are unimplemented. Build in order: Phase 1 (ingest + DB) → 2 (filter) → 3 (classify) → 4 (tracking/alerts) → 5 (résumé tailoring) → 6 (assisted form fill via Playwright).
