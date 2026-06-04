"""Structures payloads and sends contact rows securely to the Notion API.

Handles deduplication (skip/update existing contacts), payload construction,
and per-row rate limiting.
"""

import time

import requests

import config


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_API_TOKEN}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(value) -> list:
    """Build a Notion rich_text array, respecting the 2000-char block limit.

    Coerces non-string values to str so an unexpected field shape (e.g. a
    number or list coming back from Swapcard) never crashes payload building.
    """
    if value is None or value == "":
        return []
    text = value if isinstance(value, str) else str(value)
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[:2000]}}]


def build_properties(contact: dict) -> dict:
    """Map a flat contact dict to Notion property objects."""
    properties = {
        config.PROP_NAME: {"title": _rich_text(contact.get("name") or "Unknown")},
        config.PROP_COMPANY: {"rich_text": _rich_text(contact.get("company"))},
        config.PROP_ROLE: {"rich_text": _rich_text(contact.get("role"))},
        config.PROP_NOTES: {"rich_text": _rich_text(contact.get("notes"))},
        config.PROP_IGBLIVE: {"checkbox": True},
    }
    linkedin = contact.get("linkedin")
    if linkedin:
        properties[config.PROP_LINKEDIN] = {"url": str(linkedin)}
    return properties


def find_existing(name: str | None) -> str | None:
    """Return the page id of an existing contact with this Name, or None."""
    if not name:
        return None
    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    body = {
        "filter": {"property": config.PROP_NAME, "title": {"equals": name}},
        "page_size": 1,
    }
    resp = requests.post(
        url, headers=_headers(), json=body, timeout=config.REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def create_contact(contact: dict) -> dict:
    url = f"{config.NOTION_API_URL}/pages"
    payload = {
        "parent": {"database_id": config.NOTION_DATABASE_ID},
        "properties": build_properties(contact),
    }
    resp = requests.post(
        url, headers=_headers(), json=payload, timeout=config.REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def update_contact(page_id: str, contact: dict) -> dict:
    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    payload = {"properties": build_properties(contact)}
    resp = requests.patch(
        url, headers=_headers(), json=payload, timeout=config.REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def sync_contact(contact: dict) -> str:
    """Insert or update a single contact, respecting the rate-limit interval.

    Returns one of: 'created', 'updated', 'error'.
    """
    name = contact.get("name")
    status = "error"
    try:
        existing_id = find_existing(name)
        if existing_id:
            update_contact(existing_id, contact)
            status = "updated"
        else:
            create_contact(contact)
            status = "created"
    except requests.RequestException as exc:
        detail = ""
        if exc.response is not None:
            detail = f" ({exc.response.status_code}: {exc.response.text[:200]})"
        print(f"  [notion] error syncing '{name}': {exc}{detail}")
    except Exception as exc:  # one bad attendee must not abort the whole run
        print(f"  [notion] unexpected error syncing '{name}': {exc}")
    finally:
        # Respect Notion's structural API thresholds between row insertions.
        time.sleep(config.ROW_INSERT_INTERVAL)
    return status
