# AJAP — Automated Job Application Pipeline

A self-hosted intelligence system that continuously monitors **800+ company job boards**, filters and classifies every listing with an LLM, and surfaces only the roles worth applying to — running 24/7 on a Raspberry Pi.

> **What it is:** end-to-end job-search automation — discovery, filtering, LLM classification, résumé routing, and a review dashboard with real-time Discord alerts.
>
> **What it deliberately isn't:** an auto-submitter. The final click stays with the user. No anti-bot evasion, no CAPTCHA defeat — the submit step being human is an intentional design decision.

---

## How it works

```
Greenhouse API  ─┐
Lever API       ─┤─▶  Pre-filter  ─▶  Gemini 2.5 Flash  ─▶  Match  ─▶  Discord alert
SimplifyJobs    ─┘    (regex)         (classify + route)     (fit score)      │
                           │                                                   ▼
                       SQLite ◀─────────────────────────────────────── Flask dashboard
```

Every 30 minutes the daemon:

1. **Ingests** 583 Greenhouse boards + 231 Lever boards concurrently (20 workers), plus the SimplifyJobs new-grad/internship feed — ~52 000 listings per cycle in under 2 minutes
2. **Deduplicates** via `SHA-256(source_id)` — never processes the same listing twice
3. **Pre-filters** with regex rules: drops senior/staff/lead titles, non-engineering departments, and non-US locations before any LLM call
4. **Classifies** survivors with Gemini 2.5 Flash into `DATA_ENGINEERING` / `MLOPS_MLE` / `GENERAL_SWE` / `IGNORE`, with structured JSON output and cost tracking
5. **Matches** each passing role against résumé variants from `profile.json`, scoring `STRONG` / `MEDIUM` / `WEAK` fit with a reason
6. **Alerts** via Discord webhook for every new match; stamps `alerted_at` so nothing is ever double-sent

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Scheduling | APScheduler (blocking, interval + jitter) |
| HTTP | httpx (sync + async-style concurrent fetching) |
| Concurrency | `ThreadPoolExecutor` — 20 workers for Greenhouse, 15 for Lever |
| LLM | Google Gemini 2.5 Flash (`google-genai` SDK) |
| Storage | SQLite with trigger-maintained `timestamp_updated` |
| Dashboard | Flask + Jinja2 + Bootstrap 5 |
| Alerts | Discord webhook |
| Deployment | systemd services on Raspberry Pi 5 (aarch64), remote access via Tailscale |

---

## Dashboard

A local Flask dashboard (port 5001) lets you review and track every classified role:

- Filter by career track, fit score, résumé variant, role type, source, and status
- Sort by discovery time (default), post date, fit, or company
- One-click status updates (♥ Interested / ✓ Applied / ✗ Skip) with live stat counter sync
- ★ Strong Fits shortlist — unreviewed STRONG matches, freshest first

---

## Project structure

```
ajap/
├── ajap/
│   ├── config.py        # .env + profile.json loader
│   ├── db.py            # SQLite schema, migrations, query helpers
│   ├── ingest.py        # concurrent board + feed polling, dedup, normalization
│   ├── filter.py        # regex pre-filter (title / dept / location)
│   ├── enrich.py        # JD fetch for listings missing descriptions
│   ├── classify.py      # Gemini classification + cost tracking
│   ├── match.py         # résumé fit scoring
│   ├── notify.py        # Discord / email alerts
│   └── daemon.py        # APScheduler pipeline loop
├── dashboard.py         # Flask review UI
├── templates/           # Jinja2 templates (base, index, shortlist)
├── config/
│   ├── profile.example.json   # résumé paths, preferences (template)
│   └── sources.json           # board lists (Greenhouse tokens, Lever slugs)
├── tests/
├── ajap-daemon.service        # systemd unit
├── ajap-dashboard.service     # systemd unit
├── .env.example
└── requirements.txt
```

---

## Setup

```bash
# 1. Clone
git clone https://github.com/KonradT22/ajap.git && cd ajap

# 2. Virtual environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env                              # add Gemini API key + Discord webhook
cp config/profile.example.json config/profile.json   # add résumé paths + preferences

# 4. Run
python main.py --daemon      # start the polling daemon
python dashboard.py          # start the review dashboard (localhost:5001)
```

### Deploy as systemd services (Linux / Raspberry Pi)

```bash
sudo cp ajap-daemon.service ajap-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ajap-daemon ajap-dashboard
```

---

## Configuration

**`.env`** — secrets (never committed):
```
GEMINI_API_KEY=...
DISCORD_WEBHOOK_URL=...
POLL_INTERVAL_MINUTES=30
```

**`config/profile.json`** — personal preferences (never committed):
```json
{
  "resumes": {
    "swe": "resumes/swe.pdf",
    "data": "resumes/data.pdf"
  },
  "locations": ["New York", "San Francisco", "Remote"],
  "role_types": ["new_grad", "internship"]
}
```

---

## Security

- All secrets in `.env` (gitignored)
- `config/profile.json`, `resumes/`, and `data/` are gitignored — no PII in version control
- Only public job listing text is sent to the LLM; no personal data leaves the machine
