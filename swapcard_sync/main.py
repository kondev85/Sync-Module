"""Core controller: paginated Swapcard request loop, nested data extraction,
and safe, humanized delays before handing rows to the Notion sync layer.
"""

import random
import time

import requests

import config
import notion_sync

# GraphQL query targeting the Event People list view. Page size is injected
# from config so the batch size stays adjustable in one place.
GRAPHQL_QUERY = """
query GetEventPeople($viewId: ID!, $after: String, $searchable: String) {
  view(id: $viewId) {
    id
    ... on Core_EventPeopleListView {
      people(after: $after, searchable: $searchable, first: %d) {
        nodes {
          id
          userId
          firstName
          lastName
          organization
          biography
          jobTitle
          socialNetworks {
            type
            link
          }
          fields {
            definition {
              name
            }
            value
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
        totalCount
      }
    }
  }
}
""" % config.PAGE_SIZE


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


def fetch_page(after: str | None) -> dict:
    """Fetch a single page of attendees. Returns the `people` connection object."""
    variables = {
        "viewId": config.SWAPCARD_VIEW_ID,
        "after": after,
        "searchable": None,
    }
    body = {"query": GRAPHQL_QUERY, "variables": variables}
    resp = requests.post(
        config.SWAPCARD_GRAPHQL_URL,
        headers=_swapcard_headers(),
        json=body,
        timeout=config.REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errors"):
        raise RuntimeError(f"Swapcard GraphQL errors: {data['errors']}")

    view = (data.get("data") or {}).get("view")
    if not view or "people" not in view:
        raise RuntimeError(
            "Unexpected Swapcard response shape — no `view.people` found. "
            "Check the view id and query."
        )
    return view["people"]


def extract_role(node: dict) -> str | None:
    """Find the job title in the custom `fields` array, falling back to jobTitle."""
    for field in node.get("fields") or []:
        definition = field.get("definition") or {}
        name = (definition.get("name") or "").strip().lower()
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

    after: str | None = None
    page_num = 0
    totals = {"created": 0, "updated": 0, "error": 0}
    total_count: int | None = None

    while True:
        page_num += 1
        try:
            people = fetch_page(after)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"[swapcard] failed to fetch page {page_num}: {exc}")
            break

        if total_count is None:
            total_count = people.get("totalCount")
            if total_count is not None:
                print(f"Event reports {total_count} attendees total.")

        nodes = people.get("nodes") or []
        print(f"Page {page_num}: processing {len(nodes)} attendees")

        for node in nodes:
            contact = map_node(node)
            status = notion_sync.sync_contact(contact)
            totals[status] = totals.get(status, 0) + 1

        page_info = people.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        # Guard against a missing or non-advancing cursor, which would
        # otherwise re-fetch the same page forever.
        if not next_cursor or next_cursor == after:
            print("  [swapcard] cursor did not advance; stopping to avoid a loop.")
            break
        after = next_cursor

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
