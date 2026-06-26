"""Global settings, timing constraints, and credential handling.

All sensitive values are read from environment variables (Replit Secrets or a
local `.env` file in the project root). Nothing here is hardcoded — see
validate() for the required secrets.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load `.env` from the project root so secrets work outside Replit. Path is
# relative to this file (swapcard_sync/config.py -> ../.env), not the shell cwd.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# === Credentials (loaded from environment / Replit Secrets / .env) ===
SWAPCARD_BEARER_TOKEN = os.environ.get("SWAPCARD_BEARER_TOKEN")
SWAPCARD_COOKIE = os.environ.get("SWAPCARD_COOKIE")
NOTION_API_TOKEN = os.environ.get("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

# Legacy Google Programmable Search vars. The LinkedIn enricher used to call the
# Custom Search JSON API, but it now uses DuckDuckGo (no API key required), so
# these are no longer read by any code path. Kept only for backward compat /
# reference; safe to remove from Secrets.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID")

# Serper.dev API key — required only when SEARCH_BACKEND=serper.
# Register at https://serper.dev/ and set via Replit Secrets or a local .env.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

# === Swapcard GraphQL API ===
SWAPCARD_GRAPHQL_URL = "https://api.swapcard.com/graphql"
# The event view to scrape. Overridable via env, defaults to the provided view.
SWAPCARD_VIEW_ID = os.environ.get("SWAPCARD_VIEW_ID", "RXZlbnRWaWV3XzEyNjYyNzU=")

# Swapcard serves the people list via a *persisted* GraphQL query (APQ): the
# client sends only an operation name + sha256 hash, and the server resolves the
# registered query. Both are overridable via env so they can be refreshed if
# Swapcard redeploys their web client (which rotates the hash) without a code
# change. Re-capture from the browser's Network tab if requests start 400ing
# with "PersistedQueryNotFound".
SWAPCARD_OPERATION_NAME = os.environ.get(
    "SWAPCARD_OPERATION_NAME", "EventPeopleListViewConnectionQuery"
)
SWAPCARD_PERSISTED_QUERY_HASH = os.environ.get(
    "SWAPCARD_PERSISTED_QUERY_HASH",
    "c5db6335ec685ffb07963360466f639262d04d8c5cbaa89e5f5992ee20bb6579",
)

# Per-attendee profile (detail) query. The list cards omit jobTitle and
# socialNetworks, so we call this persisted query once per person to enrich Role
# + LinkedIn (+ biography). Same APQ mechanism; hash rotates on Swapcard
# redeploys, so all of these are env-overridable.
SWAPCARD_EVENT_ID = os.environ.get("SWAPCARD_EVENT_ID", "RXZlbnRfNDM5NTcyNw==")
SWAPCARD_DETAIL_OPERATION_NAME = os.environ.get(
    "SWAPCARD_DETAIL_OPERATION_NAME", "EventPersonDetailsQuery"
)
SWAPCARD_DETAIL_PERSISTED_QUERY_HASH = os.environ.get(
    "SWAPCARD_DETAIL_PERSISTED_QUERY_HASH",
    "7b56a396195a35eea892cd3c8a4aab3e0aa705042b314674375dc8abde6b5f30",
)
# Fetch each attendee's full profile? Adds one request per attendee but fills
# Role + LinkedIn + bio. Set ENRICH_PROFILES=0 for a faster name+company-only run.
ENRICH_PROFILES = os.environ.get("ENRICH_PROFILES", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)

# === Notion API ===
NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# === Timing constraints ===
# Randomized pause between pagination requests (seconds).
PAGE_DELAY_MIN = float(os.environ.get("PAGE_DELAY_MIN", "1.5"))
PAGE_DELAY_MAX = float(os.environ.get("PAGE_DELAY_MAX", "4.0"))
# Fixed interval between individual Notion row insertions (seconds).
ROW_INSERT_INTERVAL = max(0.0, float(os.environ.get("ROW_INSERT_INTERVAL", "0.3")))

# === Network ===
REQUEST_TIMEOUT = 30  # seconds
# Base wait (seconds) before retrying a rate-limited (429) or 5xx Notion call.
# Used as-is for 429 when no Retry-After header is present, and as the base for
# exponential backoff on 5xx.
NOTION_RETRY_WAIT = max(0.0, float(os.environ.get("NOTION_RETRY_WAIT", "2.0")))
NOTION_MAX_RETRIES = max(0, int(os.environ.get("NOTION_MAX_RETRIES", "5")))
# Same idea for Swapcard. With profile enrichment on, a full run makes ~1 detail
# call per attendee (thousands), so transient 429/5xx must be retried instead of
# silently dropping that row's Role/LinkedIn.
SWAPCARD_RETRY_WAIT = max(0.0, float(os.environ.get("SWAPCARD_RETRY_WAIT", "2.0")))
SWAPCARD_MAX_RETRIES = max(0, int(os.environ.get("SWAPCARD_MAX_RETRIES", "5")))

# === Run limit ===
# Optional cap on how many attendees to process in one run (for safe test
# batches). 0 / unset means no limit (sync everyone). Overridable via env.
MAX_CONTACTS = int(os.environ.get("MAX_CONTACTS", "0"))
# Optional offset: skip the first N attendees (in list order) before processing.
# Lets a run be split into chunks (e.g. SKIP_CONTACTS=60 MAX_CONTACTS=40 handles
# rows 61-100) so a long enriched run can complete within tight time limits.
SKIP_CONTACTS = max(0, int(os.environ.get("SKIP_CONTACTS", "0")))

# === Entry mode ===
# Skip main.py's interactive menu for scripted/non-interactive runs. Set
# RUN_MODE=scraper or RUN_MODE=enricher to run that job directly (e.g. piped
# commands, cron). Unset = show the [1]/[2] menu.
RUN_MODE = os.environ.get("RUN_MODE", "").strip().lower()

# === Gemini API (shared by company evaluator, Telegram bot, and LinkedIn enricher) ===
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL    = os.environ.get("GEMINI_SCOUT_MODEL", "gemini-2.5-flash-lite")

# === LinkedIn enricher — search backend ===
# Which search backend to use for the LinkedIn enricher.
#   "ddg"    — DuckDuckGo (free, no key required, rate-limited)
#   "serper" — Serper.dev (paid, requires SERPER_API_KEY, more reliable)
#   "gemini" — Gemini API with Google Search grounding (uses GEMINI_API_KEY,
#              same key as the bot/evaluator — no extra signup required)
# Overridable via env (SEARCH_BACKEND=gemini) for scripted/non-interactive runs.
# The interactive menu in main.py sets this at runtime.
SEARCH_BACKEND = os.environ.get("SEARCH_BACKEND", "ddg").strip().lower()

# Set DEBUG_ENRICHER=1 to print every query, every result href/title, and the
# exact guard (no-linkedin-url / name-mismatch / no-company-corroboration) that
# rejected each candidate. Useful for diagnosing unexpected "no match" results.
DEBUG_ENRICHER = os.environ.get("DEBUG_ENRICHER", "").strip() in ("1", "true", "yes")

# Hard stop after this many searches per run. Override via env: MAX_LOOKUPS=1000
# for bigger batches, or MAX_LOOKUPS=0 to remove the cap entirely.
_max_lookups_raw = int(os.environ.get("MAX_LOOKUPS", "300"))
MAX_LOOKUPS = max(0, _max_lookups_raw)  # 0 = unlimited

# === DuckDuckGo pacing (used when SEARCH_BACKEND=ddg) ===
# DDG is a scraped search engine with aggressive, unpredictable throttling.
# Defaults are conservative: slow pace, heavy jitter, long cooldowns, burst breaks.
# Falls back to the legacy GOOGLE_LOOKUP_INTERVAL env name if unset.
SEARCH_INTERVAL = max(
    0.0,
    float(
        os.environ.get(
            "SEARCH_INTERVAL", os.environ.get("GOOGLE_LOOKUP_INTERVAL", "5.0")
        )
    ),
)
# Randomised pacing: each wait is SEARCH_INTERVAL * (1 +/- SEARCH_JITTER).
# High jitter (40%) makes the cadence look less mechanical, reducing throttling.
SEARCH_JITTER = min(0.9, max(0.0, float(os.environ.get("SEARCH_JITTER", "0.4"))))
# Adaptive back-off: escalates with consecutive failures, resets on clean search.
# Cooldown only kicks in after SEARCH_COOLDOWN_AFTER consecutive errors.
SEARCH_COOLDOWN = max(0.0, float(os.environ.get("SEARCH_COOLDOWN", "30")))
SEARCH_COOLDOWN_MAX = max(
    SEARCH_COOLDOWN, float(os.environ.get("SEARCH_COOLDOWN_MAX", "180"))
)
SEARCH_COOLDOWN_AFTER = max(1, int(os.environ.get("SEARCH_COOLDOWN_AFTER", "2")))
SEARCH_COOLDOWN_JITTER = min(
    0.9, max(0.0, float(os.environ.get("SEARCH_COOLDOWN_JITTER", "0.3")))
)
# Proactive burst break: after every SEARCH_BURST_SIZE queries pause
# SEARCH_BURST_BREAK seconds to reset DDG's session context. 0 = disabled.
SEARCH_BURST_SIZE = max(0, int(os.environ.get("SEARCH_BURST_SIZE", "40")))
SEARCH_BURST_BREAK = max(0.0, float(os.environ.get("SEARCH_BURST_BREAK", "300")))

# === Serper pacing (used when SEARCH_BACKEND=serper) ===
# Serper is a paid REST API (google.serper.dev) — far more reliable than DDG.
# Defaults reflect Serper's own recommendations: fast pace, minimal jitter,
# short cooldowns, no proactive burst breaks (they handle quota server-side).
# Override any of these with SERPER_* env vars if needed.
SERPER_SEARCH_INTERVAL = max(
    0.0, float(os.environ.get("SERPER_SEARCH_INTERVAL", "1.0"))
)
# Small jitter (10%) is enough to avoid a perfectly mechanical cadence.
SERPER_SEARCH_JITTER = min(
    0.9, max(0.0, float(os.environ.get("SERPER_SEARCH_JITTER", "0.1")))
)
# Cooldown after a 429 / transient error: much shorter than DDG because Serper
# recovers quickly and rarely rate-limits on paid plans.
SERPER_SEARCH_COOLDOWN = max(
    0.0, float(os.environ.get("SERPER_SEARCH_COOLDOWN", "5.0"))
)
SERPER_SEARCH_COOLDOWN_MAX = max(
    SERPER_SEARCH_COOLDOWN,
    float(os.environ.get("SERPER_SEARCH_COOLDOWN_MAX", "30.0")),
)
# Wait for more consecutive errors before cooling down (Serper errors are rare).
SERPER_SEARCH_COOLDOWN_AFTER = max(
    1, int(os.environ.get("SERPER_SEARCH_COOLDOWN_AFTER", "3"))
)
SERPER_SEARCH_COOLDOWN_JITTER = min(
    0.9, max(0.0, float(os.environ.get("SERPER_SEARCH_COOLDOWN_JITTER", "0.2")))
)
# Burst breaks disabled by default for Serper — no need to protect against
# session-based throttling that DDG applies. Set SERPER_SEARCH_BURST_SIZE > 0
# to re-enable if you observe 429s on high-volume runs.
SERPER_SEARCH_BURST_SIZE = max(
    0, int(os.environ.get("SERPER_SEARCH_BURST_SIZE", "0"))
)
SERPER_SEARCH_BURST_BREAK = max(
    0.0, float(os.environ.get("SERPER_SEARCH_BURST_BREAK", "0"))
)

# === Gemini enricher pacing (used when SEARCH_BACKEND=gemini) ===
# Gemini API manages its own quota — we only need a light inter-request pause
# to avoid saturating the batch. Much faster than DDG, no burst-break needed.
GEMINI_SEARCH_INTERVAL = max(
    0.0, float(os.environ.get("GEMINI_SEARCH_INTERVAL", "1.0"))
)
GEMINI_SEARCH_JITTER = min(
    0.9, max(0.0, float(os.environ.get("GEMINI_SEARCH_JITTER", "0.1")))
)
GEMINI_SEARCH_COOLDOWN = max(
    0.0, float(os.environ.get("GEMINI_SEARCH_COOLDOWN", "10.0"))
)
GEMINI_SEARCH_COOLDOWN_MAX = max(
    10.0, float(os.environ.get("GEMINI_SEARCH_COOLDOWN_MAX", "60.0"))
)
GEMINI_SEARCH_COOLDOWN_AFTER = max(
    1, int(os.environ.get("GEMINI_SEARCH_COOLDOWN_AFTER", "3"))
)
GEMINI_SEARCH_COOLDOWN_JITTER = min(
    0.9, max(0.0, float(os.environ.get("GEMINI_SEARCH_COOLDOWN_JITTER", "0.1")))
)

# === IGB Live event profile URL ===
# Base URL for attendee profile pages. The scraper appends the Swapcard person id
# (the base64 opaque id already present in every list node) to produce a direct
# link, e.g. https://event.igblive.com/event/igb-live-2026/person/RXZlbnRQZW9...
IGB_LIVE_PERSON_BASE_URL = os.environ.get(
    "IGB_LIVE_PERSON_BASE_URL",
    "https://event.igblive.com/event/igb-live-2026/person/",
)

# === Notion property names (must match the Contacts DB column names exactly) ===
PROP_NAME = "Name"
PROP_COMPANY = "Company"
PROP_ROLE = "Role"
PROP_LINKEDIN = "LinkedIn"
PROP_NOTES = "Notes"
PROP_IGBLIVE = "iGBLive"
PROP_IGB_URL = "IGB URL"
# Status column the enricher stamps so it never reprocesses a row: "Yes" when a
# verified profile was written, "Skipped" when no confident match was found
# (LinkedIn left empty). NOTE: the column name is intentionally spelled to match
# the existing Notion column ("Enreacher"). Type is multi_select in the live DB,
# but writers stay type-aware (select/status also handled).
PROP_LINKEDIN_STATUS = "LinkedIn Enreacher"
LINKEDIN_STATUS_FOUND = "Yes"
LINKEDIN_STATUS_SKIPPED = "Skipped"

# Custom-field names to match when locating the job title inside node['fields'].
# Compared case-insensitively after stripping whitespace.
JOB_TITLE_FIELD_NAMES = ("job title", "jobtitle", "title", "position", "role")


def validate() -> None:
    """Ensure required secrets are present; raise a clear error if any are missing."""
    required = {
        "SWAPCARD_BEARER_TOKEN": SWAPCARD_BEARER_TOKEN,
        "NOTION_API_TOKEN": NOTION_API_TOKEN,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment secrets: "
            + ", ".join(missing)
            + ". Set them in a `.env` file at the project root or in Replit Secrets."
        )
