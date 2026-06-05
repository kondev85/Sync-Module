"""Find missing LinkedIn profiles for Notion contacts via Google Custom Search.

Separate from the Swapcard scraper but unified under the same project: it reuses
config (credentials, timing) and notion_sync (authenticated, retrying Notion
calls). The flow is:

  1. Page through the Notion database for rows whose LinkedIn is empty.
  2. For each, Google `"Name" "Company" site:linkedin.com/in/` via the CSE
     JSON API.
  3. Take the first result whose URL contains linkedin.com/in/ and write it back
     to that row.

Guardrails: a 1s pause between lookups, a hard cap (default 95) to stay under
Google's 100/day free quota, and per-contact error isolation so one failure
never aborts the run.
"""

import time

import requests

import config
import notion_sync


def validate() -> None:
    """Ensure the secrets this enricher needs are present."""
    required = {
        "NOTION_API_TOKEN": config.NOTION_API_TOKEN,
        "NOTION_DATABASE_ID": config.NOTION_DATABASE_ID,
        "GOOGLE_API_KEY": config.GOOGLE_API_KEY,
        "GOOGLE_CSE_ID": config.GOOGLE_CSE_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment secrets: "
            + ", ".join(missing)
            + ". Set them in the Replit Secrets panel before running."
        )


def _plain_text(prop: dict | None) -> str:
    """Flatten a Notion title/rich_text property into a plain string."""
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    text = "".join(part.get("plain_text", "") for part in parts)
    return text.strip()


def _linkedin_is_empty_filter() -> dict:
    """Build the 'LinkedIn is empty' filter matching the column's actual type.

    LinkedIn is a `url` property in the expected schema, but stay schema-aware
    (a retyped column shouldn't crash the run): fall back to rich_text, and abort
    clearly for any other type (e.g. a relation) we can't query as empty/url.
    """
    schema = notion_sync.get_schema()
    actual = schema.get(config.PROP_LINKEDIN)
    if actual in ("url", "rich_text"):
        return {"property": config.PROP_LINKEDIN, actual: {"is_empty": True}}
    raise RuntimeError(
        f"Property {config.PROP_LINKEDIN!r} is type {actual!r}; this enricher can "
        "only target a 'url' (or 'rich_text') column. Change it in Notion, then "
        "re-run."
    )


def fetch_contacts_missing_linkedin() -> list[dict]:
    """Return every row with an empty LinkedIn as {page_id, name, company}.

    Pages through the whole database (100 rows/request) via the cursor so large
    databases are fully covered.
    """
    notion_sync.ensure_required_schema()
    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    body_base = {"filter": _linkedin_is_empty_filter(), "page_size": 100}

    contacts: list[dict] = []
    cursor: str | None = None
    while True:
        body = dict(body_base)
        if cursor:
            body["start_cursor"] = cursor
        resp = notion_sync._notion_request("POST", url, body)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            contacts.append(
                {
                    "page_id": page["id"],
                    "name": _plain_text(props.get(config.PROP_NAME)),
                    "company": _plain_text(props.get(config.PROP_COMPANY)),
                }
            )
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return contacts


def google_search(name: str, company: str) -> str | None:
    """Search Google for the contact's LinkedIn /in/ profile; return first match.

    Returns the first result URL that contains 'linkedin.com/in/', or None if
    the search returns no usable result.
    """
    query = f'"{name}" "{company}" site:linkedin.com/in/'
    params = {
        "key": config.GOOGLE_API_KEY,
        "cx": config.GOOGLE_CSE_ID,
        "q": query,
    }
    resp = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params=params,
        timeout=config.REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    if not items:
        return None
    link = items[0].get("link")
    if link and "linkedin.com/in/" in link:
        return link
    return None


def save_linkedin(page_id: str, linkedin_url: str) -> None:
    """Write a LinkedIn URL back to a single contact's Notion page.

    Matches the column's actual type (url or rich_text) so the write succeeds
    regardless of how LinkedIn was configured.
    """
    schema = notion_sync.get_schema()
    actual = schema.get(config.PROP_LINKEDIN)
    if actual == "url":
        value = {"url": linkedin_url}
    elif actual == "rich_text":
        value = {"rich_text": notion_sync._rich_text(linkedin_url)}
    else:  # guarded earlier, but stay defensive
        raise RuntimeError(f"Cannot write LinkedIn to a {actual!r} property.")
    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    payload = {"properties": {config.PROP_LINKEDIN: value}}
    resp = notion_sync._notion_request("PATCH", url, payload)
    resp.raise_for_status()


def run() -> None:
    try:
        validate()
    except RuntimeError as exc:
        print(f"[enricher] {exc}")
        return

    print("Finding contacts missing a LinkedIn profile...")
    try:
        contacts = fetch_contacts_missing_linkedin()
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"[enricher] could not query Notion: {exc}")
        return

    if not contacts:
        print("No contacts are missing a LinkedIn profile. Nothing to do.")
        return

    print(
        f"Found {len(contacts)} contact(s) without LinkedIn. "
        f"Will perform up to {config.MAX_LOOKUPS} Google lookups this run."
    )

    lookups = 0
    found = 0
    skipped = 0
    errors = 0

    for contact in contacts:
        if lookups >= config.MAX_LOOKUPS:
            print(
                f"\nReached the lookup cap of {config.MAX_LOOKUPS} for this run "
                "(Google free quota safeguard). Re-run later to continue."
            )
            break

        name = contact["name"]
        company = contact["company"]

        # Per the chosen policy: only search when we have both Name and Company.
        if not name or not company:
            print(f"  [skip] '{name or '(no name)'}' — missing Name or Company.")
            skipped += 1
            continue

        try:
            lookups += 1
            link = google_search(name, company)
            if link:
                save_linkedin(contact["page_id"], link)
                found += 1
                print(f"  [found] {name} ({company}) -> {link}")
            else:
                print(f"  [none ] {name} ({company}) — no LinkedIn match.")
        except requests.RequestException as exc:
            errors += 1
            detail = ""
            if exc.response is not None:
                detail = f" ({exc.response.status_code}: {exc.response.text[:200]})"
            print(f"  [warn ] '{name}' lookup failed: {exc}{detail}")
        except Exception as exc:  # one bad contact must not abort the whole run
            errors += 1
            print(f"  [warn ] '{name}' unexpected error: {exc}")
        finally:
            # Keep request pacing clean between Google lookups.
            time.sleep(config.GOOGLE_LOOKUP_INTERVAL)

    print("\nEnrichment complete.")
    print(f"  lookups: {lookups}")
    print(f"  found:   {found}")
    print(f"  skipped: {skipped} (missing Name/Company)")
    print(f"  errors:  {errors}")


if __name__ == "__main__":
    run()
