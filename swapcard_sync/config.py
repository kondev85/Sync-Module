"""Global settings, timing constraints, and credential handling.

All sensitive values are read from environment variables (Replit Secrets).
Nothing here is hardcoded — see validate() for the required secrets.
"""

import os

# === Credentials (loaded from environment / Replit Secrets) ===
SWAPCARD_BEARER_TOKEN = os.environ.get("SWAPCARD_BEARER_TOKEN")
SWAPCARD_COOKIE = os.environ.get("SWAPCARD_COOKIE")
NOTION_API_TOKEN = os.environ.get("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

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

# === Notion property names (must match the Contacts DB column names exactly) ===
PROP_NAME = "Name"
PROP_COMPANY = "Company"
PROP_ROLE = "Role"
PROP_LINKEDIN = "LinkedIn"
PROP_NOTES = "Notes"
PROP_IGBLIVE = "iGBLive"

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
            + ". Set them in the Replit Secrets panel before running."
        )
