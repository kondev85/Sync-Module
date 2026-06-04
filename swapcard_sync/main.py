"""Core controller: paginated Swapcard request loop, nested data extraction,
and safe, humanized delays before handing rows to the Notion sync layer.
"""

import random
import time

import requests

import config
import notion_sync


def _swapcard_headers() -> dict:
    # Tolerate a token pasted with the "Bearer " prefix already included so we
    # don't end up sending "Authorization: Bearer Bearer <token>".
    token = (config.SWAPCARD_BEARER_TOKEN or "").strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer "):].strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if config.SWAPCARD_COOKIE:
        headers["Cookie"] = config.SWAPCARD_COOKIE
    return headers


def _swapcard_post(payload: list, context: str) -> dict:
    """POST a batched Swapcard GraphQL request with retry/backoff.

    Retries 429 (honoring Retry-After) and 5xx / timeouts with bounded
    exponential backoff, then returns the first batch entry (a dict). Raises on
    exhausted retries, non-retryable HTTP errors, or an unexpected batch shape.
    GraphQL-level `errors` are left for the caller to interpret.
    """
    attempt = 0
    while True:
        try:
            resp = requests.post(
                config.SWAPCARD_GRAPHQL_URL,
                headers=_swapcard_headers(),
                json=payload,
                timeout=config.REQUEST_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= config.SWAPCARD_MAX_RETRIES:
                raise
            wait = config.SWAPCARD_RETRY_WAIT * (2**attempt)
            print(f"  [swapcard] {context}: {exc.__class__.__name__}, "
                  f"retry {attempt + 1}/{config.SWAPCARD_MAX_RETRIES} in {wait:.1f}s")
            time.sleep(wait)
            attempt += 1
            continue

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt >= config.SWAPCARD_MAX_RETRIES:
                resp.raise_for_status()
            if resp.status_code == 429:
                header = resp.headers.get("Retry-After")
                try:
                    wait = float(header) if header is not None else config.SWAPCARD_RETRY_WAIT
                except ValueError:
                    wait = config.SWAPCARD_RETRY_WAIT
            else:
                wait = config.SWAPCARD_RETRY_WAIT * (2**attempt)
            print(f"  [swapcard] {context}: HTTP {resp.status_code}, "
                  f"retry {attempt + 1}/{config.SWAPCARD_MAX_RETRIES} in {wait:.1f}s")
            time.sleep(wait)
            attempt += 1
            continue

        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError(
                f"Unexpected Swapcard response for {context} "
                f"(expected a batch list of objects): {str(data)[:200]}"
            )
        return data[0]


def fetch_page(end_cursor: str | None) -> dict:
    """Fetch one page of attendees via Swapcard's persisted (APQ) query.

    Swapcard's web client sends a batched request (a JSON array) carrying only
    the operation name + persisted-query hash; the server resolves the stored
    query. We replay that exact shape and advance `endCursor` per page.

    Returns the `people` connection object (nodes, pageInfo, totalCount).
    """
    payload = [
        {
            "operationName": config.SWAPCARD_OPERATION_NAME,
            "variables": {
                "viewId": config.SWAPCARD_VIEW_ID,
                "endCursor": end_cursor,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": config.SWAPCARD_PERSISTED_QUERY_HASH,
                }
            },
        }
    ]
    entry = _swapcard_post(payload, context="people list")

    if entry.get("errors"):
        raise RuntimeError(
            f"Swapcard GraphQL errors: {entry['errors']}. "
            "If this says PersistedQueryNotFound, the query hash is stale — "
            "re-capture SWAPCARD_PERSISTED_QUERY_HASH from the browser."
        )

    view = (entry.get("data") or {}).get("view")
    if not view or "people" not in view:
        raise RuntimeError(
            "Unexpected Swapcard response shape — no `view.people` found."
        )
    return view["people"]


def extract_role(node: dict) -> str | None:
    """Find the job title in the custom `fields` array, falling back to jobTitle.

    `fields` is a union type, so the label may live under a few different keys
    depending on the field variant. We probe the common ones defensively.
    """
    for field in node.get("fields") or []:
        if not isinstance(field, dict):
            continue
        definition = field.get("definition") or field.get("field") or {}
        name = (
            definition.get("name")
            or definition.get("label")
            or field.get("name")
            or field.get("label")
            or ""
        ).strip().lower()
        if name in config.JOB_TITLE_FIELD_NAMES:
            value = field.get("value")
            if value:
                return value if isinstance(value, str) else str(value)
    return node.get("jobTitle")


def linkedin_url(profile: str | None) -> str | None:
    """Normalize a Swapcard LinkedIn value into a full profile URL.

    Swapcard stores just the handle (e.g. "ramiyermiya"); we expand it to
    https://www.linkedin.com/in/ramiyermiya. Already-full URLs pass through, and
    common prefixes ("/", "in/", "linkedin.com/...") are handled.
    """
    if not profile:
        return None
    p = str(profile).strip()
    if not p:
        return None
    low = p.lower()
    # Already a full URL (scheme check is case-insensitive) — pass through as-is.
    if low.startswith("http://") or low.startswith("https://"):
        return p
    p = p.lstrip("/")
    low = p.lower()
    # Bare domain without scheme, e.g. "linkedin.com/in/foo".
    if low.startswith("linkedin.com") or low.startswith("www.linkedin.com"):
        return "https://" + p
    # An explicit LinkedIn path segment ("in/", "company/", "school/", "pub/"):
    # keep the path verbatim rather than forcing it under "/in/".
    if "/" in p and low.split("/", 1)[0] in ("in", "company", "school", "pub"):
        if not p.split("/", 1)[1].strip("/"):
            return None
        return f"https://www.linkedin.com/{p}"
    # Otherwise treat it as a bare personal handle.
    handle = p.strip("/")
    if not handle:
        return None
    return f"https://www.linkedin.com/in/{handle}"


def extract_linkedin(social_networks: list | None) -> str | None:
    """Return a formatted LinkedIn URL from a socialNetworks array, if present."""
    for network in social_networks or []:
        if (network.get("type") or "").upper() == "LINKEDIN":
            return linkedin_url(network.get("profile") or network.get("link"))
    return None


def fetch_person_detail(person_id: str, user_id: str | None) -> dict:
    """Fetch one attendee's full profile via the persisted detail query.

    Returns the `person` object (jobTitle, socialNetworks, biography, ...).
    """
    payload = [
        {
            "operationName": config.SWAPCARD_DETAIL_OPERATION_NAME,
            "variables": {
                "skipMeetings": False,
                "withEvent": True,
                "withHostedBuyerView": False,
                "personId": person_id,
                "userId": user_id or "",
                "eventId": config.SWAPCARD_EVENT_ID,
                "viewId": "",
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": config.SWAPCARD_DETAIL_PERSISTED_QUERY_HASH,
                }
            },
        }
    ]
    entry = _swapcard_post(payload, context="person detail")
    if entry.get("errors"):
        raise RuntimeError(
            f"Swapcard detail GraphQL errors: {entry['errors']}. "
            "If PersistedQueryNotFound, re-capture "
            "SWAPCARD_DETAIL_PERSISTED_QUERY_HASH from the browser."
        )
    return (entry.get("data") or {}).get("person") or {}


def enrich_contact(contact: dict, node: dict) -> None:
    """Fetch the attendee's profile and fill Role / LinkedIn / Notes in place.

    Failures are non-fatal: the contact keeps its list-derived values so a single
    profile fetch error never drops the row.
    """
    person_id = node.get("id")
    if not person_id:
        return
    try:
        detail = fetch_person_detail(person_id, node.get("userId"))
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"  [swapcard] profile fetch failed for {contact['name']!r}: {exc}")
        return
    job_title = detail.get("jobTitle")
    if job_title:
        contact["role"] = job_title
    link = extract_linkedin(detail.get("socialNetworks"))
    if link:
        contact["linkedin"] = link
    biography = detail.get("biography")
    if biography:
        contact["notes"] = biography


def map_node(node: dict) -> dict:
    """Flatten a deep Swapcard attendee node into the Notion contact shape."""
    first = node.get("firstName") or ""
    last = node.get("lastName") or ""
    return {
        "name": f"{first} {last}".strip(),
        "company": node.get("organization"),
        "role": extract_role(node),
        "linkedin": extract_linkedin(node.get("socialNetworks")),
        "notes": node.get("biography"),
    }


def run() -> None:
    config.validate()
    print("Starting Swapcard -> Notion sync...")

    # Inspect the target Notion DB up front: abort on a missing required Name
    # property, and warn about any other field we can't map.
    try:
        notion_sync.ensure_required_schema()
        notion_sync.report_schema_mismatches()
    except requests.RequestException as exc:
        print(f"[notion] could not read database schema: {exc}")
        return
    except RuntimeError as exc:
        print(f"[notion] {exc}")
        return

    limit = config.MAX_CONTACTS or None
    if limit:
        print(f"Test mode: stopping after {limit} attendees.")
    skip = config.SKIP_CONTACTS
    if skip:
        print(f"Skipping the first {skip} attendees (resume/chunk offset).")

    end_cursor: str | None = None
    page_num = 0
    seen = 0
    processed = 0
    totals = {"created": 0, "updated": 0, "error": 0}
    total_count: int | None = None

    while True:
        page_num += 1
        try:
            people = fetch_page(end_cursor)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"[swapcard] failed to fetch page {page_num}: {exc}")
            break

        if total_count is None:
            total_count = people.get("totalCount")
            if total_count is not None:
                print(f"Event reports {total_count} attendees total.")

        nodes = people.get("nodes") or []
        print(f"Page {page_num}: processing {len(nodes)} attendees")

        # One-time probe: if enrichment is on, confirm the detail query actually
        # works before grinding through thousands of rows. A misconfigured
        # event id / stale hash would otherwise silently blank Role+LinkedIn for
        # everyone (enrich failures are non-fatal per row).
        if page_num == 1 and config.ENRICH_PROFILES and nodes:
            probe = nodes[0]
            try:
                fetch_person_detail(probe.get("id"), probe.get("userId"))
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                print(
                    "[swapcard] WARNING: profile enrichment probe failed — "
                    "Role/LinkedIn/Notes may be blank for every row.\n"
                    f"  {exc}\n"
                    "  Check SWAPCARD_EVENT_ID and SWAPCARD_DETAIL_PERSISTED_QUERY_HASH, "
                    "or set ENRICH_PROFILES=0 to skip enrichment."
                )

        reached_limit = False
        for node in nodes:
            if seen < skip:
                seen += 1
                continue
            seen += 1
            try:
                contact = map_node(node)
            except Exception as exc:  # noqa: BLE001 - one bad node must not abort the page
                print(f"  [map] skipping malformed attendee: {exc}")
                totals["error"] += 1
                continue
            if config.ENRICH_PROFILES:
                enrich_contact(contact, node)
            status = notion_sync.sync_contact(contact)
            totals[status] = totals.get(status, 0) + 1
            processed += 1
            # Absolute index (`seen`) doubles as the resume offset: if the run is
            # interrupted, re-run with SKIP_CONTACTS set to the last index shown.
            print(f"  [{seen}] {status:8} {(contact['name'] or '(no name)')[:45]}")
            if limit and processed >= limit:
                print(f"  reached test limit of {limit} attendees.")
                reached_limit = True
                break

        if reached_limit:
            break

        page_info = people.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        # Guard against a missing or non-advancing cursor, which would
        # otherwise re-fetch the same page forever.
        if not next_cursor or next_cursor == end_cursor:
            print("  [swapcard] cursor did not advance; stopping to avoid a loop.")
            break
        end_cursor = next_cursor

        # Humanized pause between pagination requests.
        delay = random.uniform(config.PAGE_DELAY_MIN, config.PAGE_DELAY_MAX)
        print(f"  ...pausing {delay:.1f}s before next page")
        time.sleep(delay)

    print("\nSync complete.")
    print(f"  created: {totals['created']}")
    print(f"  updated: {totals['updated']}")
    print(f"  errors:  {totals['error']}")


if __name__ == "__main__":
    run()
