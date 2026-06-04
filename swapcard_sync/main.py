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
    resp = requests.post(
        config.SWAPCARD_GRAPHQL_URL,
        headers=_swapcard_headers(),
        json=payload,
        timeout=config.REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    # Batched response: a list with one entry per operation.
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            f"Unexpected Swapcard response (expected a batch list): {str(data)[:200]}"
        )
    entry = data[0]
    if not isinstance(entry, dict):
        raise RuntimeError(
            f"Unexpected Swapcard batch entry (expected an object): {str(entry)[:200]}"
        )

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


def extract_linkedin(node: dict) -> str | None:
    """Find the LinkedIn URL inside the socialNetworks array."""
    for network in node.get("socialNetworks") or []:
        if (network.get("type") or "").upper() == "LINKEDIN":
            link = network.get("link")
            if link:
                return link
    return None


def map_node(node: dict) -> dict:
    """Flatten a deep Swapcard attendee node into the Notion contact shape."""
    first = node.get("firstName") or ""
    last = node.get("lastName") or ""
    return {
        "name": f"{first} {last}".strip(),
        "company": node.get("organization"),
        "role": extract_role(node),
        "linkedin": extract_linkedin(node),
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

    end_cursor: str | None = None
    page_num = 0
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

        reached_limit = False
        for node in nodes:
            try:
                contact = map_node(node)
            except Exception as exc:  # noqa: BLE001 - one bad node must not abort the page
                print(f"  [map] skipping malformed attendee: {exc}")
                totals["error"] += 1
                continue
            status = notion_sync.sync_contact(contact)
            totals[status] = totals.get(status, 0) + 1
            processed += 1
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
