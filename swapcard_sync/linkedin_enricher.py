"""Find missing LinkedIn profiles for Notion contacts via DuckDuckGo search.

Separate from the Swapcard scraper but unified under the same project: it reuses
config (timing) and notion_sync (authenticated, retrying Notion calls). The flow
is:

  1. Page through the Notion database for rows whose LinkedIn is empty.
  2. For each, search DuckDuckGo for `"Name" "Company" site:linkedin.com/in/`.
  3. Take the first result whose URL contains linkedin.com/in/ and write it back
     to that row.

DuckDuckGo needs no API key or quota project — unlike the Google CSE it replaced
— but it rate-limits bursts, so we pace requests (config.SEARCH_INTERVAL) and
keep a per-run cap (config.MAX_LOOKUPS). Per-contact error isolation means one
failure never aborts the run.
"""

import time

import requests
from ddgs import DDGS
from ddgs.exceptions import DDGSException

import config
import notion_sync


def validate() -> None:
    """Ensure the secrets this enricher needs are present.

    DuckDuckGo needs no credentials, so only the Notion secrets are required.
    """
    required = {
        "NOTION_API_TOKEN": config.NOTION_API_TOKEN,
        "NOTION_DATABASE_ID": config.NOTION_DATABASE_ID,
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


def search_linkedin(name: str, company: str) -> str | None:
    """Search DuckDuckGo for the contact's LinkedIn /in/ profile; first match.

    Returns the first result URL that contains 'linkedin.com/in/', or None if
    none of the results do. We scan results in order rather than only the very
    first item: even with a `site:linkedin.com/in/` query, the top hit can
    occasionally be a non-/in/ URL (e.g. a /pub/ or company link), so we take
    the first genuine profile match.
    """
    query = f'"{name}" "{company}" site:linkedin.com/in/'
    with DDGS(timeout=config.REQUEST_TIMEOUT) as ddgs:
        results = ddgs.text(query, max_results=10)
    for item in results or []:
        link = item.get("href")
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
        f"Will perform up to {config.MAX_LOOKUPS} DuckDuckGo searches this run."
    )

    lookups = 0
    found = 0
    skipped = 0
    errors = 0

    for contact in contacts:
        if lookups >= config.MAX_LOOKUPS:
            print(
                f"\nReached the search cap of {config.MAX_LOOKUPS} for this run "
                "(rate-limit safeguard). Re-run later to continue."
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
            link = search_linkedin(name, company)
            if link:
                save_linkedin(contact["page_id"], link)
                found += 1
                print(f"  [found] {name} ({company}) -> {link}")
            else:
                print(f"  [none ] {name} ({company}) — no LinkedIn match.")
        except DDGSException as exc:
            errors += 1
            print(f"  [warn ] '{name}' search failed (DuckDuckGo): {exc}")
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
            # Keep request pacing clean between searches (DuckDuckGo throttles).
            time.sleep(config.SEARCH_INTERVAL)

    print("\nEnrichment complete.")
    print(f"  lookups: {lookups}")
    print(f"  found:   {found}")
    print(f"  skipped: {skipped} (missing Name/Company)")
    print(f"  errors:  {errors}")


if __name__ == "__main__":
    run()
