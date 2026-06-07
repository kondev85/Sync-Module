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

import random
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


# Generic corporate words that carry no identifying signal, so they must not be
# what we corroborate a name-only match against (e.g. a stray "consulting" in an
# unrelated person's headline would otherwise count as a company hit).
_COMPANY_STOPWORDS = {
    "ltd", "limited", "inc", "llc", "gmbh", "co", "corp", "corporation", "group",
    "holding", "holdings", "the", "and", "consulting", "ecommerce", "com", "plc",
    "ag", "sa", "srl", "bv", "oy", "ab", "as", "kft", "solutions", "services",
    "company", "international", "global", "agency", "studio", "media", "digital",
    # Generic employment statuses — never an identifying employer, so they must
    # not corroborate a match (otherwise any "self-employed" stranger qualifies).
    "self", "employed", "employee", "freelance", "freelancer", "freelancing",
    "independent", "consultant", "contractor", "owner", "founder", "entrepreneur",
}


def _company_variants(company: str) -> list[str]:
    """Ordered, de-duped search terms for a (possibly multi-) company field.

    Swapcard often lists two companies in one field, e.g.
    "HHK Ecommerce Consulting Ltd / vip-grinders.com" or "Taptica (Nexxen)".
    Quoting the whole string rarely matches verbatim, so we also try each part
    on its own. The full string stays first (most specific when it does hit).
    """
    company = (company or "").strip()
    out: list[str] = []
    if company:
        out.append(company)
    for part in re.split(r"[/()|,;]| - ", company):
        part = part.strip(" -")
        if part and part not in out:
            out.append(part)
    return out


def _company_tokens(company: str, min_len: int = 4) -> list[str]:
    """Identifying company tokens (>=min_len chars, minus generic corporate words).

    Defaults to >=4 chars to drop noise. Callers can pass min_len=2 as a fallback
    to recover short acronym employers (IBM, SAP, AWS, EY, 3M) that would
    otherwise yield no tokens at all.
    """
    tokens = re.findall(r"[a-z0-9]+", _strip_accents(company))
    return [t for t in tokens if len(t) >= min_len and t not in _COMPANY_STOPWORDS]


def _token_present(token: str, haystack: str) -> bool:
    """Whole-token (word-boundary) match, treating digits as word chars.

    Substring matching would let 'cisco' corroborate inside 'francisco', so we
    require the token to stand alone (bounded by non-alphanumerics or string
    ends). Handles digit-bearing tokens like 'big4play'/'3m' correctly, which
    plain \\b would mishandle around digit/letter transitions.
    """
    pattern = r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _company_corroborated(company: str, title: str, body: str) -> bool:
    """True if an identifying company token appears in the profile's own text.

    We check BOTH the result title ("Real Name - Headline | LinkedIn") and the
    snippet/body (an excerpt of the actual profile page). This is the safeguard
    against same-name-different-company matches: DuckDuckGo only loosely honors a
    quoted company, so a query like `"Zubair Bhatti" "HPC Consultancy"` can still
    return a *different* Zubair Bhatti. Requiring the company to actually surface
    in the returned profile — not just in our query — rejects those impostors.

    Searching the body is safe here precisely because it is an excerpt of the
    matched page: if the company isn't on that person's profile it won't appear,
    so a wrong-company profile can't corroborate. (This differs from NAME
    matching, where the body is untrustworthy because it echoes the searched
    company; see name_matches_profile.)

    Tokens are matched on word boundaries (not raw substrings) so 'cisco' can't
    corroborate inside 'francisco'. Strong tokens are >=4 chars; if a company has
    none (short acronym employers like IBM/SAP/EY/3M), we fall back to >=2-char
    tokens so those aren't always skipped.

    If the company still has no identifying tokens (all generic, e.g. "Self
    employed"), corroboration is impossible, so we return False and skip rather
    than risk writing a stranger.
    """
    tokens = _company_tokens(company)
    if not tokens:
        # Short-acronym fallback (IBM, EY, 3M, SAP, AWS) so they aren't lost.
        tokens = _company_tokens(company, min_len=2)
    if not tokens:
        return False
    haystack = _strip_accents(title or "") + " " + _strip_accents(body or "")
    return any(_token_present(token, haystack) for token in tokens)


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


def search_linkedin(name: str, company: str) -> tuple[str | None, int]:
    """Search DuckDuckGo for the contact's verified LinkedIn /in/ profile.

    Tries several queries in order of confidence and returns the first /in/ URL
    that matches the contact on BOTH axes:

      * name — the profile's slug or title must contain the contact's name
        (see name_matches_profile); AND
      * company — an identifying company token must actually appear in the
        returned profile's title or snippet (see _company_corroborated).

    Queries tried: the full company string, then each listed company on its own
    (Swapcard often packs two companies into one field — "A Ltd / b.com",
    "A (B)" — and quoting the whole thing rarely matches), then a final
    name-only query as a last resort.

    Crucially, putting the company in the query is NOT enough to accept a hit:
    DuckDuckGo only loosely honors quotes, so `"Name" "Company"` can still return
    a *different* person who shares the name but works elsewhere. We therefore
    require the company to be corroborated *in the result itself* for every
    query, name-only or not — this is what stops same-name/wrong-company URLs
    from being written.

    Returns `(url_or_None, queries_run)`. `queries_run` lets the caller charge
    every actual DuckDuckGo request against MAX_LOOKUPS, since one contact can
    now cost several searches. url is None when nothing matches confidently
    (caller marks it "Skipped"). Transient rate-limit/timeout (and other
    non-"no results" DDGS) errors are raised so the caller leaves the row for a
    later retry instead of Skipping it.
    """
    attempts: list[str] = [
        f'"{name}" "{variant}" site:linkedin.com/in/'
        for variant in _company_variants(company)
    ]
    # Name-only last resort: still requires company corroboration in the result.
    attempts.append(f'"{name}" site:linkedin.com/in/')

    queries_run = 0
    for index, query in enumerate(attempts):
        if index:
            _paced_sleep()  # pace DuckDuckGo between queries
        queries_run += 1
        results = _ddg_text(query)
        if results is None:
            continue  # genuine "no results" for this query — try the next one
        for item in results:
            link = item.get("href")
            if not (link and "linkedin.com/in/" in link):
                continue
            title = item.get("title")
            body = item.get("body")
            if not name_matches_profile(name, link, title):
                continue
            # Always confirm the company surfaces in the actual profile, never
            # just because we searched for it — guards against same-name matches.
            if _company_corroborated(company, title, body):
                return link, queries_run
    return None, queries_run


def _paced_sleep() -> None:
    """Wait SEARCH_INTERVAL between searches, with optional +/- jitter.

    A perfectly steady drumbeat is the easiest pattern for DuckDuckGo to flag and
    throttle, so we randomise each gap by SEARCH_JITTER to look less mechanical.
    """
    base = config.SEARCH_INTERVAL
    if base <= 0:
        return
    if config.SEARCH_JITTER:
        base *= 1 + random.uniform(-config.SEARCH_JITTER, config.SEARCH_JITTER)
    time.sleep(max(0.0, base))


def _ddg_text(query: str) -> list | None:
    """Run one DuckDuckGo text search.

    Returns the result list, or None for a genuine "no results" (a real
    no-match). Re-raises rate-limit/timeout and any other DDGS failure so the
    caller treats it as transient (retry) rather than a permanent Skip.
    """
    try:
        with DDGS(timeout=config.REQUEST_TIMEOUT) as ddgs:
            return ddgs.text(query, max_results=10) or []
    except (RatelimitException, TimeoutException):
        raise
    except DDGSException as exc:
        if "no results" in str(exc).lower():
            return None
        raise


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

    max_lookups = config.MAX_LOOKUPS  # may be overridden by interactive prompt
    if max_lookups == 0:
        print(
            f"Found {len(contacts)} contact(s) without LinkedIn. "
            "No search cap — will process the full list."
        )
    else:
        print(
            f"Found {len(contacts)} contact(s) without LinkedIn. "
            f"Will perform up to {max_lookups} DuckDuckGo searches this run."
        )

    lookups = 0  # actual DuckDuckGo queries run (a contact can cost several)
    found = 0
    no_match = 0
    skipped = 0
    errors = 0
    consecutive_errors = 0  # drives the adaptive cooldown (gentle on DuckDuckGo)

    for contact in contacts:
        if max_lookups and lookups >= max_lookups:
            print(
                f"\nReached the search cap of {max_lookups} for this run "
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
            link, queries_run = search_linkedin(name, company)
            lookups += queries_run
            # DuckDuckGo answered cleanly — reset the back-off here (not after the
            # Notion write) so the streak reflects DDG health only, not Notion's.
            consecutive_errors = 0
            # Proactive burst break: every SEARCH_BURST_SIZE queries we pause
            # SEARCH_BURST_BREAK seconds before DDG starts throttling us. This
            # is cheaper than waiting for timeouts to accumulate.
            burst = config.SEARCH_BURST_SIZE
            if (
                burst > 0
                and config.SEARCH_BURST_BREAK > 0
                and lookups % burst == 0
            ):
                print(
                    f"\n  [burst] {lookups} searches done — pausing "
                    f"{config.SEARCH_BURST_BREAK:.0f}s to reset DuckDuckGo "
                    "session context (proactive burst break)."
                )
                time.sleep(config.SEARCH_BURST_BREAK)
                print("  [burst] Resuming.\n")
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
            consecutive_errors += 1
            print(f"  [warn ] '{name}' transient search error (will retry): {exc}")
            # Adaptive back-off: pause (escalating with consecutive timeouts) so
            # DuckDuckGo can recover instead of being poked at the same rhythm.
            cooldown = min(
                config.SEARCH_COOLDOWN * consecutive_errors,
                config.SEARCH_COOLDOWN_MAX,
            )
            if cooldown > 0:
                print(
                    f"  [cool ] backing off {cooldown:.0f}s after "
                    f"{consecutive_errors} consecutive timeout(s) to let "
                    "DuckDuckGo recover."
                )
                time.sleep(cooldown)
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
            _paced_sleep()

    print("\nEnrichment complete.")
    print(f"  lookups:  {lookups}")
    print(f"  found:    {found} (written + marked Yes)")
    print(f"  no match: {no_match} (marked Skipped, left empty)")
    print(f"  skipped:  {skipped} (missing Name/Company, left for retry)")
    print(f"  errors:   {errors} (transient, left for retry)")


if __name__ == "__main__":
    run()
