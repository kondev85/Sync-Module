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


# Property name -> the Notion type this script knows how to write.
EXPECTED_TYPES = {
    config.PROP_NAME: "title",
    config.PROP_COMPANY: "rich_text",
    config.PROP_ROLE: "rich_text",
    config.PROP_NOTES: "rich_text",
    config.PROP_LINKEDIN: "url",
    config.PROP_IGBLIVE: "checkbox",
}

_schema_cache: dict | None = None


def get_schema() -> dict:
    """Fetch (and cache) the target database's {property_name: type} map."""
    global _schema_cache
    if _schema_cache is None:
        url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}"
        resp = requests.get(url, headers=_headers(), timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        _schema_cache = {name: meta.get("type") for name, meta in props.items()}
    return _schema_cache


def ensure_required_schema() -> None:
    """Abort early if the required Name(title) property is missing or wrong type.

    Name backs both dedup (find_existing) and every page's identity, so without
    it the run would degrade into an error on every single row.
    """
    schema = get_schema()
    if schema.get(config.PROP_NAME) != "title":
        raise RuntimeError(
            f"Required property {config.PROP_NAME!r} must be a 'title' property, "
            f"but the database has {schema.get(config.PROP_NAME)!r}. Cannot sync."
        )


def report_schema_mismatches() -> None:
    """Warn (once, up front) about mapped properties that can't be written."""
    schema = get_schema()
    for prop, expected in EXPECTED_TYPES.items():
        actual = schema.get(prop)
        if actual is None:
            print(
                f"  [notion] NOTE: property {prop!r} not found in the database — "
                f"those values will be skipped. Add a {expected} property named "
                f"{prop!r} if you want them synced."
            )
        elif actual != expected:
            print(
                f"  [notion] NOTE: property {prop!r} is type {actual!r} but this "
                f"script writes {expected!r} — it will be skipped to avoid errors."
            )


def build_properties(contact: dict) -> dict:
    """Map a flat contact dict to Notion property objects.

    Only includes properties that actually exist in the database with the
    expected type, so a schema drift (missing/renamed/retyped column) skips that
    field instead of failing the whole row.
    """
    schema = get_schema()

    def usable(prop: str, expected: str) -> bool:
        return schema.get(prop) == expected

    properties: dict = {}
    if usable(config.PROP_NAME, "title"):
        properties[config.PROP_NAME] = {
            "title": _rich_text(contact.get("name") or "Unknown")
        }
    for prop, key in (
        (config.PROP_COMPANY, "company"),
        (config.PROP_ROLE, "role"),
        (config.PROP_NOTES, "notes"),
    ):
        if usable(prop, "rich_text"):
            properties[prop] = {"rich_text": _rich_text(contact.get(key))}
    if usable(config.PROP_LINKEDIN, "url") and contact.get("linkedin"):
        properties[config.PROP_LINKEDIN] = {"url": str(contact["linkedin"])}
    if usable(config.PROP_IGBLIVE, "checkbox"):
        properties[config.PROP_IGBLIVE] = {"checkbox": True}
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
