from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FILTERS_PATH = Path("config/filters.json")

# Terms that, if found as a whole word in a lowercased location string, indicate
# non-US. Matched with \b...\b word boundaries, not substring `in` — a plain
# substring check previously matched "india" inside "Indiana" and "mexico"
# inside "New Mexico", silently rejecting real US postings in those states.
#
# Poland is deliberately excluded: the user is authorized to work there, so
# Polish listings should pass this gate same as US ones.
_NON_US_MARKERS: tuple[str, ...] = (
    "canada", "uk", "united kingdom", "england", "scotland", "wales",
    "germany", "france", "india", "singapore", "australia", "china",
    "hong kong", "ireland", "japan", "netherlands", "sweden", "spain",
    "portugal", "norway", "denmark", "finland", "switzerland", "austria",
    "belgium", "brazil", "mexico", "israel", "taiwan", "south korea",
    "new zealand", "united arab emirates",
    "pakistan", "romania", "italy", "russia", "ukraine", "greece", "turkey",
    "czech republic", "czechia", "hungary", "vietnam", "indonesia",
    "malaysia", "philippines", "thailand", "egypt", "colombia", "argentina",
    "chile", "south africa", "saudi arabia", "qatar", "bangladesh",
    "luxembourg", "estonia", "latvia", "lithuania", "iceland", "slovakia",
    "slovenia", "croatia", "serbia", "bulgaria", "nigeria", "kenya",
)

# Bare city names (board postings routinely give the city with no country, e.g.
# "Munich" or "London") for major non-US tech hubs that have no US namesake a
# real new-grad SWE board would plausibly use. Kept separate from the country
# list above because this is a riskier, deliberately curated set — each one
# was checked against the live DB for a real US namesake before inclusion.
#
# Deliberately NOT included despite being non-US-leaning, because they also
# name a real US tech hub and would cause false rejects: Cambridge (MA — MIT/
# biotech hub), Dublin (CA/OH — Bay Area + Columbus tech), Bristol (CT/TN/VA),
# Manchester (NH), Birmingham (AL), Melbourne (FL — aerospace/defense hub),
# Geneva (IL — Chicago suburb), Vienna (VA — DC-area hub). "London" (Ohio) and
# "Paris" (Texas) are included anyway since those namesakes are tiny towns
# with no realistic tech-hiring presence.
_NON_US_CITY_MARKERS: tuple[str, ...] = (
    "london", "paris", "berlin", "amsterdam", "toronto", "munich",
    "stockholm", "montreal", "tokyo", "bangalore", "bengaluru", "hyderabad",
    "prague", "oakville", "madrid", "barcelona", "zurich", "milan",
    "copenhagen", "oslo", "seoul", "beijing", "shanghai", "sydney",
)

_NON_US_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _NON_US_MARKERS) + r")\b",
    re.IGNORECASE,
)
_NON_US_CITY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _NON_US_CITY_MARKERS) + r")\b",
    re.IGNORECASE,
)

# Vague tags board APIs use alongside a real location (e.g. speechify's board
# pairs a specific city with the literal string "Distributed"). These carry no
# location signal of their own and must not count as a "this location is
# US-plausible" vote — otherwise ["Vienna, Austria", "Distributed"] passes
# the gate purely because "Distributed" isn't flagged non-US, regardless of
# what the real location says.
_PLACEHOLDER_LOCATIONS: frozenset[str] = frozenset({
    "distributed", "remote", "hybrid", "various", "various locations",
    "n/a", "worldwide", "home based - worldwide",
})
_N_LOCATIONS_RE = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)


def _is_placeholder_location(loc: str) -> bool:
    normalized = loc.strip().lower()
    return normalized in _PLACEHOLDER_LOCATIONS or bool(_N_LOCATIONS_RE.match(normalized))

# US postings are virtually always ASCII/Latin text. A location string with
# CJK, Hangul, Cyrillic, Arabic, Hebrew, or Thai characters (e.g. a Tokyo
# office address given in Japanese) is a reliable non-US signal that no
# English marker string would catch.
_NON_LATIN_RE = re.compile(
    "["
    "぀-ヿ"  # Hiragana, Katakana
    "㐀-䶿"  # CJK extension A
    "一-鿿"  # CJK unified ideographs
    "가-힣"  # Hangul syllables
    "Ѐ-ӿ"  # Cyrillic
    "؀-ۿ"  # Arabic
    "֐-׿"  # Hebrew
    "฀-๿"  # Thai
    "]"
)


# Workday board postings format many locations as "City, XXX" with an ISO-3166
# alpha-3 country code (e.g. "Bengaluru, IND", "Dubai, ARE") instead of a spelled-out
# country name, which the marker list above doesn't catch. US locations from the
# same source use 2-letter state codes or the literal "USA", never a 3-letter code.
_ISO3_SUFFIX_RE = re.compile(r",\s*([A-Z]{3})\s*$")


# "New Mexico" is a US state whose name contains the country "Mexico" as a
# genuine whole word, so the word-boundary marker check alone can't tell them
# apart — strip it before matching.
_NEW_MEXICO_RE = re.compile(r"\bnew mexico\b", re.IGNORECASE)

# A trailing ", <state>" with an unambiguous US state abbreviation is a
# stronger, more specific signal than any word/city marker — it overrides
# city markers that happen to also name a real US place: "Berlin, CT" (a real
# Hartford-area town), "New Berlin, WI" (a real Milwaukee suburb), "New
# London, CT" (home of General Dynamics Electric Boat), "Turkey, TX" (a real,
# if tiny, town) all share a word with a non-US marker but are unambiguously
# US once the state code is checked.
#
# CA and DE are deliberately excluded: both are live collisions with a real
# country code in this exact ", XX" suffix position in the actual feed data
# — "Sydney, CA" (Canada) and "Munich, DE" (Germany) — so trusting them here
# would un-reject genuinely foreign listings. Every other state code was
# checked against the feed and found no such collision.
_US_STATE_SUFFIX_RE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CO|CT|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|"
    r"WA|WV|WI|WY)\s*$",
    re.IGNORECASE,
)

# Some boards (tenstorrent, axq) describe a multi-office role as one
# semicolon-joined string, e.g. "Austin, Texas, United States; Toronto,
# Ontario, Canada" — a real US office sits right there, but a marker
# matching elsewhere in that same string (toronto/canada) would otherwise
# flag the whole thing non-US. An explicit "United States"/"USA" anywhere in
# the string is unambiguous enough to override that.
_US_LITERAL_RE = re.compile(r"\bunited states\b|\busa\b", re.IGNORECASE)


def _is_non_us(loc: str) -> bool:
    """True if a single location string is clearly not US."""
    if _US_LITERAL_RE.search(loc):
        return False
    if _US_STATE_SUFFIX_RE.search(loc.strip()):
        return False
    stripped = _NEW_MEXICO_RE.sub("", loc)
    if _NON_US_RE.search(stripped):
        return True
    if _NON_US_CITY_RE.search(stripped):
        return True
    if _NON_LATIN_RE.search(loc):
        return True
    m = _ISO3_SUFFIX_RE.search(loc.strip())
    return bool(m and m.group(1) != "USA")


def _us_gate(job: dict) -> bool:
    """Return True (PASS) if the job is US-accessible.

    Accepts feed dict ('locations': list[str]) or DB row ('locations_raw': JSON str).
    Unspecified or empty locations → pass (high-recall).
    Only rejects when ALL listed locations are clearly non-US.
    """
    if "locations" in job:
        locs: list[str] = job["locations"] or []
    elif "locations_raw" in job and job["locations_raw"]:
        try:
            locs = json.loads(job["locations_raw"])
        except (json.JSONDecodeError, TypeError):
            locs = []
    else:
        return True  # unspecified → keep

    if not locs:
        return True

    # Vague tags like "Distributed" carry no location signal of their own —
    # drop them before voting so they can't rubber-stamp a real foreign
    # location sitting elsewhere in the same list. If that empties the list
    # (e.g. ["Remote"] alone), fall through to the same high-recall keep as
    # an empty/unspecified location list.
    informative = [loc for loc in locs if not _is_placeholder_location(loc)]
    if not informative:
        return True

    # Keep if ANY location is US-plausible (not in non-US list)
    return any(not _is_non_us(loc) for loc in informative)


def load_filters() -> dict:
    if not FILTERS_PATH.exists():
        logger.warning("No filters.json at %s — keyword gates disabled", FILTERS_PATH)
        return {"recency_days": 45, "whitelist": [], "blacklist": []}
    with FILTERS_PATH.open() as fh:
        return json.load(fh)


# Loaded once at module import; callers may override via the `filters` kwarg.
_default_filters: dict = load_filters()


def evaluate(job: dict, *, filters: dict | None = None) -> str:
    """Return 'QUEUED' or 'EVAL_REJECTED'."""
    return evaluate_with_reason(job, filters=filters)[0]


def evaluate_with_reason(
    job: dict, *, filters: dict | None = None
) -> tuple[str, str | None]:
    """Return (status, reason). reason is None when status is 'QUEUED'.

    Gates applied in order (cheapest first):
      1. ACTIVE    — listing marked inactive → 'inactive'
      2. US-ONLY   — all locations non-US → 'non-us'
      3. RECENCY   — date_posted older than recency_days → 'too-old'
      4. BLACKLIST — any blacklist term in title → 'blacklist'
      5. WHITELIST — no whitelist term in title → 'no-whitelist'

    Accepts both feed dicts (active: bool, date_posted: int unix ts,
    locations: list[str]) and DB row dicts (is_active: int 0/1,
    date_posted: ISO str, locations_raw: JSON str).
    """
    _f = filters if filters is not None else _default_filters

    # Gate 1: active status.
    if "is_active" in job:
        if not job["is_active"]:
            return "EVAL_REJECTED", "inactive"
    elif "active" in job:
        if not job["active"]:
            return "EVAL_REJECTED", "inactive"

    # Gate 2: US-only location.
    if not _us_gate(job):
        return "EVAL_REJECTED", "non-us"

    # Gate 3: recency.
    recency_days = int(_f.get("recency_days", 45))
    if recency_days > 0:
        dp_raw = job.get("date_posted")
        if dp_raw is not None:
            try:
                if isinstance(dp_raw, (int, float)):
                    date_posted = datetime.fromtimestamp(dp_raw, tz=timezone.utc)
                else:
                    date_posted = datetime.fromisoformat(str(dp_raw))
                    if date_posted.tzinfo is None:
                        date_posted = date_posted.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
                if date_posted < cutoff:
                    return "EVAL_REJECTED", "too-old"
            except (ValueError, OSError):
                pass  # unparseable timestamp — don't reject on recency

    # Resolve title from either feed dict ('title') or DB row ('job_title').
    title = (job.get("title") or job.get("job_title") or "").lower()

    # Gate 4: blacklist takes priority over whitelist.
    for term in _f.get("blacklist", []):
        if term.lower() in title:
            return "EVAL_REJECTED", "blacklist"

    # Gate 5: whitelist — must match at least one term.
    # Empty list = gate disabled; all titles pass.
    whitelist = _f.get("whitelist", [])
    if whitelist and not any(term.lower() in title for term in whitelist):
        return "EVAL_REJECTED", "no-whitelist"

    return "QUEUED", None
