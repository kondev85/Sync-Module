"""Schema-aware, fill-empty-only Notion writes and ordered SiGMA deduplication."""

import re
import time
import unicodedata
from urllib.parse import urlparse

import config
import notion_sync

FIELDS = {
    config.PROP_NAME: ("name", ("title",)),
    config.PROP_COMPANY: ("company", ("rich_text",)),
    config.PROP_ROLE: ("role", ("rich_text",)),
    config.PROP_NOTES: ("notes", ("rich_text",)),
    config.PROP_LINKEDIN: ("linkedin", ("url",)),
    "Website Domain": ("website", ("url",)),
    "Email": ("email", ("email", "rich_text")),
    "SIGMA URL": ("sigma_url", ("url",)),
    "SiGMAWorld": ("sigma", ("checkbox",)),
    "Business Type": ("business_type", ("multi_select",)),
    "Status": ("status", ("select", "status", "multi_select", "rich_text")),
}
OVERWRITE = {"SIGMA URL", "SiGMA URL", "SiGMAWorld", "Business Type"}
SUFFIXES = {"ltd", "limited", "plc", "inc", "llc", "group", "gmbh", "sa", "srl", "bv", "co"}


def value(prop: dict) -> str:
    kind = prop.get("type") or next(
        (k for k in ("title", "rich_text", "url", "email", "select", "status") if k in prop), ""
    )
    data = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(p.get("plain_text", p.get("text", {}).get("content", "")) for p in data or []).strip()
    if kind in ("select", "status"):
        return (data or {}).get("name", "")
    return str(data).strip() if data is not None else ""


def normalized_company(company: str) -> str:
    company = unicodedata.normalize("NFKC", company).casefold()
    company = re.sub(r"[^\w\s]", "", company)
    return " ".join(word for word in company.split() if word not in SUFFIXES)


def name_key(first: str, last: str, company: str) -> tuple | None:
    first = " ".join(first.casefold().split())
    initial = next((c.casefold() for c in last if c.isalpha()), "")
    company = normalized_company(company)
    return (first, initial, company) if first and initial and company else None


def url_key(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return host + parsed.path.rstrip("/").casefold()


def rich_text(content: str) -> list:
    # Preserve all Notes, not just the shared helper's first 2,000 characters.
    return [
        {"type": "text", "text": {"content": content[i:i + 2000]}}
        for i in range(0, len(content), 2000)
    ]


def property_name(name: str, schema: dict) -> str:
    """Use Lead Pool's uppercase column; accept the original mixed-case brief."""
    if name == "SIGMA URL" and schema.get(name) != "url" and schema.get("SiGMA URL") == "url":
        return "SiGMA URL"
    return name


def build_properties(contact: dict, schema: dict, existing: dict | None = None) -> dict:
    result = {}
    for name, (key, allowed) in FIELDS.items():
        name = property_name(name, schema)
        kind = schema.get(name)
        if kind not in allowed:
            continue
        old = (existing or {}).get("properties", {}).get(name, {})
        # Never change an existing title, notes, status, email, or other filled
        # property. Only source attribution fields are explicit exceptions.
        if existing is not None and name not in OVERWRITE and value(old):
            continue
        if existing is not None and name == "Status":
            continue  # New/Check duplicate are creation statuses, not updates.
        incoming = True if key == "sigma" else contact.get(key)
        if not incoming and key != "sigma":
            continue
        if kind in ("rich_text", "title"):
            result[name] = {kind: rich_text(str(incoming))}
        elif kind in ("url", "email", "checkbox"):
            result[name] = {kind: incoming}
        elif kind in ("select", "status"):
            result[name] = {kind: {"name": incoming}}
        elif kind == "multi_select":
            choices = [incoming] if isinstance(incoming, str) else incoming
            result[name] = {kind: [{"name": n} for n in dict.fromkeys(choices) if n]}
    return result


class NotionImporter:
    def __init__(self):
        notion_sync.clear_schema_cache()
        notion_sync.ensure_required_schema()
        self.schema = notion_sync.get_schema()
        for name, (_, allowed) in FIELDS.items():
            name = property_name(name, self.schema)
            actual = self.schema.get(name)
            if actual not in allowed:
                print(f"  [notion] {name!r}: {actual or 'missing'}; skipped (expected {'/'.join(allowed)}).")
        self.pages = {}
        self.by_sigma = {}
        self.by_linkedin = {}
        self.by_name = {}
        self._load()

    def _index(self):
        self.by_sigma, self.by_linkedin, self.by_name = {}, {}, {}
        for page in self.pages.values():
            props = page.get("properties", {})
            linkedin = url_key(value(props.get(config.PROP_LINKEDIN, {})))
            name = value(props.get(config.PROP_NAME, {})).rsplit(None, 1)
            key = name_key(name[0], name[1], value(props.get(config.PROP_COMPANY, {}))) if len(name) == 2 else None
            for source_column in ("SIGMA URL", "SiGMA URL"):
                sigma = url_key(value(props.get(source_column, {})))
                if sigma:
                    self.by_sigma.setdefault(sigma, []).append(page["id"])
            for index, token in ((self.by_linkedin, linkedin), (self.by_name, key)):
                if token:
                    index.setdefault(token, []).append(page["id"])

    def _load(self):
        cursor = None
        seen = set()
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            response = notion_sync._notion_request(
                "POST", f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query", body
            )
            response.raise_for_status()
            data = response.json()
            for page in data.get("results", []):
                self.pages[page["id"]] = page
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen:
                raise RuntimeError("Notion pagination did not advance; cannot safely check duplicates.")
            seen.add(cursor)
            time.sleep(config.ROW_INSERT_INTERVAL)
        self._index()
        print(f"  [notion] Indexed {len(self.pages)} existing rows for duplicate checks.")

    def already_imported(self, sigma_url):
        return bool(self.by_sigma.get(url_key(sigma_url)))

    def sync(self, contact: dict) -> tuple[str, str]:
        if self.already_imported(contact["sigma_url"]):
            return "skipped", "SIGMA URL already imported"
        matches = self.by_linkedin.get(url_key(contact.get("linkedin", "")), [])
        reason = "LinkedIn match"
        if not matches:
            matches = self.by_name.get(
                name_key(contact["first_name"], contact["last_name"], contact["company"]), []
            )
            reason = "First name + surname initial + company"
        if len(matches) == 1:
            # Refresh before merging so a human edit since indexing is preserved.
            response = notion_sync._notion_request("GET", f"{config.NOTION_API_URL}/pages/{matches[0]}")
            response.raise_for_status()
            existing = response.json()
            properties = build_properties(contact, self.schema, existing)
            response = notion_sync._notion_request(
                "PATCH", f"{config.NOTION_API_URL}/pages/{matches[0]}",
                {"properties": properties}
            )
            status, note = "updated", reason
        else:
            contact = dict(contact)
            contact["status"] = "New"
            status, note = "created", "No duplicate match"
            if len(matches) > 1:
                names = [value(self.pages[pid]["properties"].get(config.PROP_NAME, {})) for pid in matches]
                note = "Possible duplicates: " + "; ".join(names)
                contact["notes"] = (contact.get("notes", "") + "\n\n" + note).strip()
                contact["status"] = "Check duplicate"
                status = "check-duplicate"
            response = notion_sync._notion_request(
                "POST", f"{config.NOTION_API_URL}/pages",
                {"parent": {"database_id": config.NOTION_DATABASE_ID},
                 "properties": build_properties(contact, self.schema)}, idempotent=False
            )
        # Never retry ambiguous creation failures: the shared helper deliberately
        # avoids retrying non-idempotent POSTs on 5xx.
        try:
            response.raise_for_status()
        finally:
            time.sleep(config.ROW_INSERT_INTERVAL)
        page = response.json()
        self.pages[page["id"]] = page
        self._index()
        return status, note
