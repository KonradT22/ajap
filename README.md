# AJAP — Job-Search Command Center

A local-first pipeline that **discovers, filters, classifies, and tracks** new-grad and
off-cycle software / data-infrastructure roles — so the tedious 90% of a job hunt is automated
and your attention goes only to the roles worth your time.

> **What this is:** an intelligence-gathering and tracking system. It aggregates public
> listings, filters them against your criteria, routes them by career track, matches the right
> résumé, and keeps state on everything you've seen.
>
> **What this deliberately is not:** an auto-submitter. The final "Submit" on any application
> stays with you. No anti-bot evasion, no fingerprint spoofing, no CAPTCHA defeat — those
> break platform terms, get you flagged at the firms you most want, and produce low-quality
> applications. Leaving the submit step to a human is the design decision that keeps this both
> defensible and effective.

## Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Ingest     │ -> │  Pre-filter  │ -> │   Classify   │ -> │   Review /   │
│ (poll feeds) │    │   (regex)    │    │ (Gemini LLM) │    │   Tracking   │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
        │                  │                   │                   │
        └──────────────────┴─── SQLite (state) ┴───────────────────┘
                                    │
                          Discord / email alerts
```

## Project structure

```
ajap/
├── ajap/                   # application package
│   ├── __init__.py
│   ├── config.py           # load .env + profile.json        (phase 1)
│   ├── db.py               # SQLite layer + schema            (phase 1)
│   ├── ingest.py           # poll feeds, dedup, write rows    (phase 1)
│   ├── filter.py           # regex whitelist/blacklist        (phase 2)
│   ├── classify.py         # Gemini career-track routing      (phase 3)
│   └── notify.py           # Discord / email alerts           (phase 4)
├── config/
│   ├── profile.example.json
│   └── profile.json        # your real profile (gitignored)
├── data/                   # SQLite db lives here (gitignored)
├── resumes/                # your résumé PDFs (gitignored)
├── tests/
├── main.py                 # entrypoint / scheduler
├── .env.example
├── requirements.txt
└── .gitignore
```

## Setup

```bash
# 1. Clone and enter
git clone git@github.com:<you>/ajap.git && cd ajap

# 2. Virtual environment
python3 -m venv .venv && source .venv/bin/activate

# 3. Dependencies
pip install -r requirements.txt

# 4. Configuration
cp .env.example .env                       # then fill in real keys
cp config/profile.example.json config/profile.json   # then fill in your data
```

## Roadmap

- [ ] **Phase 1 — Ingestion + storage.** Poll the SimplifyJobs repo and public Greenhouse/
      Lever/Ashby boards, dedup via SHA-256(company+title), write to SQLite.
- [ ] **Phase 2 — Pre-filter.** Regex whitelist/blacklist on title; mark non-matches `EVAL_REJECTED`.
- [ ] **Phase 3 — Classification.** Route survivors into `DATA_ENGINEERING` / `MLOPS_MLE` /
      `GENERAL_SWE` / `IGNORE`; attach the matching résumé.
- [ ] **Phase 4 — Tracking + alerts.** Status lifecycle + Discord/email pings on strong matches.
- [ ] **Phase 5 — Résumé matching + tailoring.** Suggest variant and targeted edits per role.
- [ ] **Phase 6 — Assisted fill.** Pre-populate the real form from `profile.json`; you review and submit.

## Security

- All secrets live in `.env` (gitignored).
- `config/profile.json` and everything in `resumes/` and `data/` are gitignored — PII never
  enters version control.
- The job description text sent to the LLM contains no personal data; only public listing text.
