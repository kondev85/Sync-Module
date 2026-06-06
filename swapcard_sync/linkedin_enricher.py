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

import re
import time
import unicodedata

import requests
from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

import config
import notion_sync


# Name particles we ignore when matching a person to a profile, so "van"/"de"/etc
# never count as a real name token.
_NAME_PARTICLES = {
    "de", "da", "di", "du", "del", "della", "der", "den", "van", "von", "la",
    "le", "el", "al", "bin", "ibn", "dos", "das", "do", "san", "st",
}


def _strip_accents(text: str) -> str:
    """Lowercased ASCII-folded text so 'Kārlis' compares as 'karlis'."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _name_tokens(name: str) -> list[str]:
    """Split a person's name into meaningful lowercased ASCII tokens."""
    tokens = re.findall(r"[a-z]+", _strip_accents(name))
    return [t for t in tokens if len(t) >= 2 and t not in _NAME_PARTICLES]


def name_matches_profile(name: str, href: str, title: str) -> bool:
    """True only if the profile plausibly belongs to `name`.

    DuckDuckGo loosely honors quotes/`site:`, so a `site:linkedin.com/in/` query
    for an unindexed person can return an unrelated profile. To avoid writing the
    wrong LinkedIn we require BOTH the first and last name tokens to appear in the
    profile's own identity — its /in/ slug (e.g. 'dogandemir') or its result
    title (which LinkedIn renders as "Real Name - Headline | LinkedIn").

    We deliberately do NOT match against the result *snippet/body*: that text
    echoes the searched company and surrounding noise, so a different person
    named "Ahmet" at the same company would otherwise pass on a surname that only
    appears in the snippet. One-token names only need that single token.
    """
    tokens = _name_tokens(name)
    if not tokens:
        return False
    slug = ""
    match = re.search(r"/in/([^/?#]+)", href or "")
    if match:
        slug = re.sub(r"[^a-z]", "", _strip_accents(match.group(1)))
    text = _strip_accents(title or "")

    def present(token: str) -> bool:
        return token in slug or token in text

    if len(tokens) == 1:
        return present(tokens[0])
    return present(tokens[0]) and present(tokens[-1])


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


def _build_query_filter() -> dict:
    """Select rows the enricher should still try: LinkedIn empty AND not yet
    stamped in the status column.

    The status clause is what makes "Skipped" sticky — once a row is marked
    Yes/No/Skipped it's excluded from every future run, so we never re-search a
    person we already gave up on (or that a human marked). If the status column
    is missing or an unsupported type, we silently fall back to the LinkedIn-only
    filter (old behavior).
    """
    linkedin_empty = _linkedin_is_empty_filter()
    schema = notion_sync.get_schema()
    status_type = schema.get(config.PROP_LINKEDIN_STATUS)
    if status_type in ("multi_select", "select", "status"):
        return {
            "and": [
                linkedin_empty,
                {
                    "property": config.PROP_LINKEDIN_STATUS,
                    status_type: {"is_empty": True},
                },
            ]
        }
    return linkedin_empty


def fetch_contacts_missing_linkedin() -> list[dict]:
    """Return every row with an empty LinkedIn as {page_id, name, company}.

    Pages through the whole database (100 rows/request) via the cursor so large
    databases are fully covered.
    """
    notion_sync.ensure_required_schema()
    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    body_base = {"filter": _build_query_filter(), "page_size": 100}

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
    """Search DuckDuckGo for the contact's verified LinkedIn /in/ profile.

    Scans results in order and returns the first /in/ URL whose name actually
    matches the contact (see name_matches_profile) — so a loosely-related hit for
    an unindexed person is rejected rather than written. Returns None when no
    result matches confidently (treated by the caller as "Skipped"). Transient
    rate-limit/timeout errors are raised so the caller leaves the row for a later
    retry instead of marking it Skipped.
    """
    query = f'"{name}" "{company}" site:linkedin.com/in/'
    try:
        with DDGS(timeout=config.REQUEST_TIMEOUT) as ddgs:
            results = ddgs.text(query, max_results=10)
    except (RatelimitException, TimeoutException):
        raise  # transient — let run() count it as an error and retry next run
    except DDGSException as exc:
        # Only a literal "no results" is a genuine no-match (-> Skipped). Any
        # other DDGS failure (backend/parser/network hiccup) is transient, so
        # re-raise it rather than permanently stamping the row Skipped.
        if "no results" in str(exc).lower():
            return None
        raise
    for item in results or []:
        link = item.get("href")
        if link and "linkedin.com/in/" in link:
            if name_matches_profile(name, link, item.get("title")):
                return link
    return None


def _status_property(option_name: str) -> dict | None:
    """Build the status-column value matching its actual Notion type.

    Returns None if the status column is absent or an unsupported type, so the
    caller can degrade gracefully (just write/skip LinkedIn without a stamp).
    """
    status_type = notion_sync.get_schema().get(config.PROP_LINKEDIN_STATUS)
    if status_type == "multi_select":
        return {"multi_select": [{"name": option_name}]}
    if status_type == "select":
        return {"select": {"name": option_name}}
    if status_type == "status":
        return {"status": {"name": option_name}}
    return None


def record_result(page_id: str, linkedin_url: str | None) -> None:
    """Persist one contact's outcome in a single Notion PATCH.

    Found  -> write the LinkedIn URL and stamp status "Yes".
    No match -> leave LinkedIn empty and stamp status "Skipped" (so it's never
    retried). Writing the URL stays type-aware (url or rich_text).
    """
    properties: dict = {}
    if linkedin_url:
        actual = notion_sync.get_schema().get(config.PROP_LINKEDIN)
        if actual == "url":
            properties[config.PROP_LINKEDIN] = {"url": linkedin_url}
        elif actual == "rich_text":
            properties[config.PROP_LINKEDIN] = {
                "rich_text": notion_sync._rich_text(linkedin_url)
            }
        else:  # guarded earlier, but stay defensive
            raise RuntimeError(f"Cannot write LinkedIn to a {actual!r} property.")
        status = _status_property(config.LINKEDIN_STATUS_FOUND)
    else:
        status = _status_property(config.LINKEDIN_STATUS_SKIPPED)

    if status is not None:
        properties[config.PROP_LINKEDIN_STATUS] = status
    if not properties:
        return  # nothing writable (no URL and no status column) — leave as-is

    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    resp = notion_sync._notion_request(
        "PATCH", url, {"properties": properties}
    )
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
    no_match = 0
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
        # These rows are left unstamped so they retry automatically if the user
        # fills in the missing field later (they cost no search/lookup).
        if not name or not company:
            print(f"  [skip ] '{name or '(no name)'}' — missing Name or Company.")
            skipped += 1
            continue

        try:
            lookups += 1
            link = search_linkedin(name, company)
            # Stamp the outcome either way: a verified URL -> "Yes"; no confident
            # match -> "Skipped" (LinkedIn stays empty, never retried).
            record_result(contact["page_id"], link)
            if link:
                found += 1
                print(f"  [found] {name} ({company}) -> {link}")
            else:
                no_match += 1
                print(f"  [none ] {name} ({company}) — no match -> Skipped.")
        except (RatelimitException, TimeoutException) as exc:
            # Transient: do NOT stamp Skipped, so the row retries on a later run.
            errors += 1
            print(f"  [warn ] '{name}' transient search error (will retry): {exc}")
        except requests.RequestException as exc:
            errors += 1
            detail = ""
            if exc.response is not None:
                detail = f" ({exc.response.status_code}: {exc.response.text[:200]})"
            print(f"  [warn ] '{name}' Notion write failed: {exc}{detail}")
        except Exception as exc:  # one bad contact must not abort the whole run
            errors += 1
            print(f"  [warn ] '{name}' unexpected error: {exc}")
        finally:
            # Keep request pacing clean between searches (DuckDuckGo throttles).
            time.sleep(config.SEARCH_INTERVAL)

    print("\nEnrichment complete.")
    print(f"  lookups:  {lookups}")
    print(f"  found:    {found} (written + marked Yes)")
    print(f"  no match: {no_match} (marked Skipped, left empty)")
    print(f"  skipped:  {skipped} (missing Name/Company, left for retry)")
    print(f"  errors:   {errors} (transient, left for retry)")


if __name__ == "__main__":
    run()
