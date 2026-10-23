"""Paginated, capped SiGMA import runner. Never sends messages or connections."""

from collections import Counter
import os
from uuid import UUID

import config
from .client import CookieExpired, SigmaClient
from .notion_import import NotionImporter
from .profiles import build_contact, text


def print_summary(rows, filtered, offset_skipped):
    print("\nSiGMA import results")
    print(f"{'#':<5} {'Name':<30} {'Result':<17} Details")
    print("-" * 100)
    for index, name, status, note in rows:
        print(f"{index:<5} {name[:29]:<30} {status:<17} {note}")
    counts = Counter(row[2] for row in rows)
    print("\nResult             Count")
    for status in ("created", "updated", "skipped", "check-duplicate", "error"):
        print(f"{status:<19}{counts[status]}")
    print(f"Filtered out: {filtered}; skipped by offset: {offset_skipped}")


def run():
    missing = [k for k in ("SIGMA_COOKIE", "NOTION_API_TOKEN", "NOTION_DATABASE_ID") if not os.environ.get(k)]
    if missing:
        raise RuntimeError("Missing required secrets: " + ", ".join(missing))
    # Safer than the legacy scraper default: an unset/empty cap means 20.
    # Explicit MAX_CONTACTS=0 is still supported for a later authorized full run.
    cap = int(os.environ.get("MAX_CONTACTS", "").strip() or "20")
    if cap < 0:
        raise ValueError("MAX_CONTACTS must be non-negative.")
    skip = config.SKIP_CONTACTS
    types = {s.strip().casefold() for s in os.environ.get("SIGMA_BUSINESS_TYPES", "").split(",") if s.strip()}
    print(f"SiGMA import | cap: {cap or 'unlimited'} | offset: {skip} | types: {', '.join(sorted(types)) or 'all'}")
    client = SigmaClient(os.environ["SIGMA_COOKIE"])
    rows, filtered, offset_skipped = [], 0, 0
    try:
        # Validate source auth before spending time or writes on Notion.
        page = skip // 20 + 1
        items, total = client.attendees(page)
        notion = NotionImporter()
        last_page = (total + 19) // 20
        stop = False
        while page <= last_page and not stop:
            if not items and (page - 1) * 20 < total:
                raise RuntimeError("SiGMA returned an unexpectedly empty page; import stopped.")
            for offset, item in enumerate(items):
                index = (page - 1) * 20 + offset
                if index < skip:
                    offset_skipped += 1
                    continue
                business_types = item.get("business_type") or []
                if isinstance(business_types, str):
                    business_types = [business_types]
                if types and not types.intersection(text(t).casefold() for t in business_types):
                    filtered += 1
                    continue
                if cap and len(rows) >= cap:
                    stop = True
                    break
                name = (text(item.get("first_name")) + " " + text(item.get("last_name"))).strip()
                try:
                    attendee_id = str(UUID(item["id"]))
                    sigma_url = f"https://match.sigma.world/attendees/{attendee_id}"
                    if notion.already_imported(sigma_url):
                        status, note = "skipped", "SIGMA URL already imported"
                    else:
                        profile = client.profile(attendee_id)
                        if profile.get("id") and str(profile["id"]) != attendee_id:
                            raise ValueError("Profile ID differs from the requested attendee.")
                        contact = build_contact({**item, "id": attendee_id, "business_type": business_types}, profile)
                        if not contact["name"]:
                            raise ValueError("Attendee has no name; not imported.")
                        status, note = notion.sync(contact)
                except CookieExpired:
                    raise
                except Exception as exc:
                    # Do not print response bodies, request headers, or cookies.
                    status, note = "error", f"{type(exc).__name__}: import failed; row left for review/retry"
                    if isinstance(exc, (ValueError, RuntimeError)):
                        note = str(exc)
                    # A failed/ambiguous write should not cause an automatic
                    # second creation in this same run.
                    rows.append((index + 1, name, status, note))
                    raise RuntimeError(f"Stopped at attendee #{index + 1}: {note}") from None
                rows.append((index + 1, name, status, note))
                print(f"  [{index + 1}] {name}: {status} — {note}", flush=True)
                if cap and len(rows) >= cap:
                    stop = True
                    break
            page += 1
            if page <= last_page and not stop:
                items, new_total = client.attendees(page)
                last_page = (new_total + 19) // 20
        print(f"\nStopped after {len(rows)} matching attendees. No full import was started." if cap else "\nImport complete.")
    finally:
        client.close()
        print_summary(rows, filtered, skip - skip % 20 + offset_skipped)
