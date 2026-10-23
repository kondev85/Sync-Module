"""Cached Pipeline matching; the only permitted mutation is adding a Source tag."""

from collections import Counter
import time
from urllib.parse import quote, unquote

import config
import notion_sync
from .notion_import import name_key, url_key, value

SOURCE_TAG = "SiGMA2026"


def _request(method, path, body=None):
    response = notion_sync._notion_request(method, config.NOTION_API_URL + path, body)
    response.raise_for_status()
    return response.json()


def _paginate(path, body=None):
    """Fetch all records once at startup, rejecting incomplete pagination."""
    cursor, seen = None, set()
    while True:
        payload = {"page_size": 100, **(body or {})}
        if cursor:
            payload["start_cursor"] = cursor
        data = _request("POST", path, payload)
        yield from data.get("results", [])
        if not data.get("has_more"):
            return
        cursor = data.get("next_cursor")
        if not cursor or cursor in seen:
            raise RuntimeError("Pipeline pagination did not advance; refusing an incomplete cache.")
        seen.add(cursor)
        time.sleep(config.ROW_INSERT_INTERVAL)


def discover_databases():
    """Resolve exact database titles with the existing integration, without new keys."""
    found = {"pipeline contacts": [], "pipeline companies": []}
    for db in _paginate("/search", {
        "query": "Pipeline", "filter": {"value": "database", "property": "object"},
    }):
        title = value({"title": db.get("title", [])}).casefold()
        if title in found:
            found[title].append(db)
    for title, matches in found.items():
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one accessible Notion database {title!r}; found {len(matches)}. "
                "Share Pipeline Contacts and PipeLine Companies with the existing Notion integration."
            )
    return found["pipeline contacts"][0], found["pipeline companies"][0]


class PipelineCheck:
    """Use a startup snapshot for all matching; never query Pipeline per attendee."""

    def __init__(self):
        contacts_db, companies_db = discover_databases()
        props = contacts_db.get("properties", {})
        for name, expected in (("Name", "title"), ("LinkedIn", "url"),
                               ("Company", "relation"), ("Source", "multi_select")):
            if props.get(name, {}).get("type") != expected:
                raise RuntimeError(f"Pipeline Contacts requires {name!r} to be {expected}.")
        if companies_db.get("properties", {}).get("Company", {}).get("type") != "title":
            raise RuntimeError("PipeLine Companies requires a title column named 'Company'.")
        relation_db = props["Company"].get("relation", {}).get("database_id", "")
        if relation_db.replace("-", "") != companies_db["id"].replace("-", ""):
            raise RuntimeError("Pipeline Contacts.Company does not link to PipeLine Companies.")
        options = props["Source"]["multi_select"].get("options", [])
        if not any(option.get("name") == SOURCE_TAG for option in options):
            raise RuntimeError("Pipeline Contacts.Source must already contain option 'SiGMA2026'.")
        self.companies = {
            page["id"]: value(page.get("properties", {}).get("Company", {}))
            for page in _paginate(f"/databases/{companies_db['id']}/query")
        }
        self.contacts = {
            page["id"]: page
            for page in _paginate(f"/databases/{contacts_db['id']}/query")
        }
        self.counts = Counter()
        self.by_linkedin, self.by_name = {}, {}
        for pid, page in self.contacts.items():
            properties = page.get("properties", {})
            linkedin = url_key(value(properties.get("LinkedIn", {})))
            if linkedin:
                self.by_linkedin.setdefault(linkedin, set()).add(pid)
            name = value(properties.get("Name", {})).rsplit(None, 1)
            relation = properties.get("Company", {})
            linked_companies = relation.get("relation", [])
            if relation.get("has_more"):
                linked_companies = self._full_relation(pid, relation)
            for company in linked_companies:
                company_id = company["id"]
                if company_id not in self.companies:
                    raise RuntimeError(
                        "A Pipeline Company relation is not visible in the companies cache. "
                        "Check Notion sharing permissions before running."
                    )
                key = name_key(name[0], name[1], self.companies[company_id]) if len(name) == 2 else None
                if key:
                    self.by_name.setdefault(key, set()).add(pid)
        print(
            f"  [pipeline] Cached {len(self.contacts)} Pipeline Contacts and "
            f"{len(self.companies)} PipeLine Companies."
        )

    @staticmethod
    def _full_relation(pid, prop):
        """Hydrate >25 company relations during startup only, never while matching."""
        property_id = quote(unquote(prop["id"]), safe="")
        path = f"/pages/{pid}/properties/{property_id}"
        cursor, seen, relations = None, set(), []
        while True:
            suffix = f"?page_size=100&start_cursor={quote(cursor, safe='')}" if cursor else "?page_size=100"
            data = _request("GET", path + suffix)
            relations.extend(item["relation"] for item in data.get("results", []) if "relation" in item)
            if not data.get("has_more"):
                return relations
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen:
                raise RuntimeError("Pipeline company relation pagination did not advance.")
            seen.add(cursor)
            time.sleep(config.ROW_INSERT_INTERVAL)

    def match(self, contact):
        """Stop at the first rule with any hits, including ambiguous LinkedIn hits."""
        matches = self.by_linkedin.get(url_key(contact.get("linkedin", "")), set())
        if not matches:
            matches = self.by_name.get(
                name_key(contact["first_name"], contact["last_name"], contact["company"]), set()
            )
        return sorted(matches)

    def check(self, contact) -> tuple[str, str | None]:
        matches = self.match(contact)
        if not matches:
            return "pipeline not found", None
        if len(matches) > 1:
            names = [value(self.contacts[pid]["properties"].get("Name", {})) for pid in matches]
            print("  pipeline ambiguous: " + "; ".join(names))
            self.counts["pipeline ambiguous"] += 1
            return "pipeline ambiguous", None
        pid = matches[0]
        properties = self.contacts[pid]["properties"]
        name = value(properties.get("Name", {}))
        tags = properties["Source"].get("multi_select", [])
        if any(tag.get("name") == SOURCE_TAG for tag in tags):
            self.counts["pipeline already tagged"] += 1
            return "pipeline already tagged", name
        # Cached Source options are preserved. Never send any other property,
        # never POST /pages, and never delete or edit a Pipeline contact/company.
        preserved = [
            {"id": tag["id"]} if tag.get("id") else {"name": tag["name"]}
            for tag in tags
        ]
        _request("PATCH", f"/pages/{pid}", {
            "properties": {"Source": {"multi_select": preserved + [{"name": SOURCE_TAG}]}}
        })
        properties["Source"]["multi_select"] = tags + [{"name": SOURCE_TAG}]
        time.sleep(config.ROW_INSERT_INTERVAL)
        self.counts["pipeline tagged"] += 1
        return "pipeline tagged", name
