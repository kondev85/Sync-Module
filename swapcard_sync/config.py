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
# Attendees fetched per pagination request. Keep modest to stay polite.
PAGE_SIZE = int(os.environ.get("SWAPCARD_PAGE_SIZE", "50"))

# === Notion API ===
NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# === Timing constraints ===
# Randomized pause between pagination requests (seconds).
PAGE_DELAY_MIN = float(os.environ.get("PAGE_DELAY_MIN", "1.5"))
PAGE_DELAY_MAX = float(os.environ.get("PAGE_DELAY_MAX", "4.0"))
# Fixed interval between individual Notion row insertions (seconds).
ROW_INSERT_INTERVAL = 0.3

# === Network ===
REQUEST_TIMEOUT = 30  # seconds

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
