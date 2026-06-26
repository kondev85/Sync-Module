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

import json
import os
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

# Substrings that mark a DuckDuckGo/network failure as transient (retry later).
_TRANSIENT_SEARCH_KEYWORDS = (
    "timeout",
    "timed out",
    "connecttimeout",
    "readtimeout",
    "connection",
    "rate limit",
    "ratelimit",
    "temporarily unavailable",
    "unavailable",
    "503",
    "high demand",
    "winerror 10060",
    "failed to respond",
    "network",
)


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

    DuckDuckGo needs no credentials; Serper requires SERPER_API_KEY;
    Gemini requires GEMINI_API_KEY.
    """
    required: dict = {
        "NOTION_API_TOKEN": config.NOTION_API_TOKEN,
        "NOTION_DATABASE_ID": config.NOTION_DATABASE_ID,
    }
    if config.SEARCH_BACKEND == "serper":
        required["SERPER_API_KEY"] = config.SERPER_API_KEY
    if config.SEARCH_BACKEND == "gemini":
        required["GEMINI_API_KEY"] = config.GEMINI_API_KEY
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment secrets: "
            + ", ".join(missing)
            + ". Set them in a `.env` file at the project root or in Replit Secrets."
        )


def _plain_text(prop: dict | None) -> str:
    """Flatten a Notion title/rich_text property into a plain string."""
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    text = "".join(part.get("plain_text", "") for part in parts)
    return text.strip()


def _read_linkedin(props: dict) -> str:
    """Return the LinkedIn URL/text already stored on a row, if any."""
    prop = props.get(config.PROP_LINKEDIN) or {}
    actual = notion_sync.get_schema().get(config.PROP_LINKEDIN)
    if actual == "url":
        return (prop.get("url") or "").strip()
    if actual == "rich_text":
        return _plain_text(prop)
    return ""


def _read_status_names(props: dict) -> list[str]:
    """Return status-column option names already set on a row."""
    prop = props.get(config.PROP_LINKEDIN_STATUS) or {}
    status_type = notion_sync.get_schema().get(config.PROP_LINKEDIN_STATUS)
    if status_type == "multi_select":
        return [
            item.get("name", "")
            for item in (prop.get("multi_select") or [])
            if item.get("name")
        ]
    if status_type == "select":
        selected = prop.get("select") or {}
        name = selected.get("name")
        return [name] if name else []
    if status_type == "status":
        selected = prop.get("status") or {}
        name = selected.get("name")
        return [name] if name else []
    return []


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


def _build_prefilled_linkedin_filter() -> dict | None:
    """Filter for rows that already have LinkedIn but no enricher status stamp."""
    schema = notion_sync.get_schema()
    linkedin_type = schema.get(config.PROP_LINKEDIN)
    status_type = schema.get(config.PROP_LINKEDIN_STATUS)
    if linkedin_type not in ("url", "rich_text"):
        return None
    if status_type not in ("multi_select", "select", "status"):
        return None
    return {
        "and": [
            # Notion rejects `is_empty: false` on url/rich_text — use is_not_empty.
            {
                "property": config.PROP_LINKEDIN,
                linkedin_type: {"is_not_empty": True},
            },
            {
                "property": config.PROP_LINKEDIN_STATUS,
                status_type: {"is_empty": True},
            },
        ]
    }


def _stamp_status(page_id: str, option_name: str) -> None:
    """Write only the enricher status column (leave LinkedIn untouched)."""
    status = _status_property(option_name)
    if status is None:
        return
    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    resp = notion_sync._notion_request(
        "PATCH",
        url,
        {"properties": {config.PROP_LINKEDIN_STATUS: status}},
    )
    resp.raise_for_status()


def backfill_linkedin_status() -> int:
    """Stamp 'Yes' on rows that already have LinkedIn (e.g. from the scraper).

    The Swapcard scraper can populate LinkedIn without touching the enricher
    status column. Those rows are already excluded from the empty-LinkedIn query,
    but stamping them prevents confusion and keeps future runs aligned with Replit.
    """
    query_filter = _build_prefilled_linkedin_filter()
    if query_filter is None:
        return 0

    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    body_base = {"filter": query_filter, "page_size": 100}
    stamped = 0
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
            if not _read_linkedin(props):
                continue
            if _read_status_names(props):
                continue
            _stamp_status(page["id"], config.LINKEDIN_STATUS_FOUND)
            stamped += 1
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return stamped


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
            linkedin = _read_linkedin(props)
            # Belt-and-suspenders: never queue a row that already has LinkedIn or
            # a status stamp, even if Notion's server-side filter misbehaves.
            if linkedin or _read_status_names(props):
                continue
            contacts.append(
                {
                    "page_id": page["id"],
                    "name": _plain_text(props.get(config.PROP_NAME)),
                    "company": _plain_text(props.get(config.PROP_COMPANY)),
                    "linkedin": linkedin,
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
        results = _search_text(query)
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
    """Wait between searches using the active backend's interval and jitter.

    DDG needs a slow, heavily jittered cadence to avoid throttling.
    Serper and Gemini are managed APIs — a light 1 s pause is fine.
    """
    if config.SEARCH_BACKEND == "serper":
        base = config.SERPER_SEARCH_INTERVAL
        jitter = config.SERPER_SEARCH_JITTER
    elif config.SEARCH_BACKEND == "gemini":
        base = config.GEMINI_SEARCH_INTERVAL
        jitter = config.GEMINI_SEARCH_JITTER
    else:
        base = config.SEARCH_INTERVAL
        jitter = config.SEARCH_JITTER
    if base <= 0:
        return
    if jitter:
        base *= 1 + random.uniform(-jitter, jitter)
    time.sleep(max(0.0, base))


def _is_transient_search_error(exc: BaseException) -> bool:
    """True when a search failure should retry later and trigger backoff."""
    if isinstance(exc, (RatelimitException, TimeoutException)):
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    try:
        import httpx

        if isinstance(
            exc,
            (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError),
        ):
            return True
    except ImportError:
        pass

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (RatelimitException, TimeoutException)):
            return True
        if isinstance(current, (requests.Timeout, requests.ConnectionError)):
            return True
        blob = f"{type(current).__name__} {current}".lower()
        if any(keyword in blob for keyword in _TRANSIENT_SEARCH_KEYWORDS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _search_cooldown_seconds(consecutive_errors: int) -> float:
    """Escalating pause after consecutive search failures, with random jitter.

    Uses the active backend's cooldown settings — Serper/Gemini are much shorter
    than DDG's because managed APIs recover faster and errors are rarer.
    """
    if config.SEARCH_BACKEND == "serper":
        cooldown_after = config.SERPER_SEARCH_COOLDOWN_AFTER
        cooldown = config.SERPER_SEARCH_COOLDOWN
        cooldown_max = config.SERPER_SEARCH_COOLDOWN_MAX
        cooldown_jitter = config.SERPER_SEARCH_COOLDOWN_JITTER
    elif config.SEARCH_BACKEND == "gemini":
        cooldown_after = config.GEMINI_SEARCH_COOLDOWN_AFTER
        cooldown = config.GEMINI_SEARCH_COOLDOWN
        cooldown_max = config.GEMINI_SEARCH_COOLDOWN_MAX
        cooldown_jitter = config.GEMINI_SEARCH_COOLDOWN_JITTER
    else:
        cooldown_after = config.SEARCH_COOLDOWN_AFTER
        cooldown = config.SEARCH_COOLDOWN
        cooldown_max = config.SEARCH_COOLDOWN_MAX
        cooldown_jitter = config.SEARCH_COOLDOWN_JITTER
    if consecutive_errors < cooldown_after:
        return 0.0
    steps = consecutive_errors - cooldown_after + 1
    base = min(cooldown * steps, cooldown_max)
    if cooldown_jitter and base > 0:
        base *= 1 + random.uniform(-cooldown_jitter, cooldown_jitter)
    return max(0.0, base)


def _maybe_search_cooldown(consecutive_errors: int) -> None:
    """Pause with visible logging when the search backend keeps failing."""
    cooldown = _search_cooldown_seconds(consecutive_errors)
    if cooldown <= 0:
        return
    if config.SEARCH_BACKEND == "serper":
        backend_label = "Serper"
    elif config.SEARCH_BACKEND == "gemini":
        backend_label = "Gemini"
    else:
        backend_label = "DuckDuckGo"
    print(
        f"  [cool ] backing off {cooldown:.0f}s after "
        f"{consecutive_errors} consecutive search error(s) "
        f"to let {backend_label} recover."
    )
    time.sleep(cooldown)


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
        if _is_transient_search_error(exc):
            raise TimeoutException(str(exc)) from exc
        raise
    except Exception as exc:
        if _is_transient_search_error(exc):
            raise TimeoutException(str(exc)) from exc
        raise


def _serper_text(query: str) -> list | None:
    """Run one Serper.dev Google search and return results in DDG-compatible shape.

    Serper returns `{"organic": [{"title": ..., "link": ..., "snippet": ...}]}`.
    We normalise to `[{"href": ..., "title": ..., "body": ...}]` so the rest of
    the enricher logic is backend-agnostic.

    Raises RatelimitException on HTTP 429 (mirrors DDG behaviour so the shared
    cooldown/backoff logic kicks in). Raises requests.HTTPError on other failures.
    Returns None only for a genuine "no results" response (empty organic list).
    """
    api_key = config.SERPER_API_KEY
    if not api_key:
        raise RuntimeError(
            "SERPER_API_KEY is not set. Add it to Replit Secrets or your .env file."
        )
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=config.REQUEST_TIMEOUT,
        )
    except (requests.Timeout, requests.ConnectionError):
        raise
    if resp.status_code == 429:
        raise RatelimitException("Serper rate limit (HTTP 429)")
    resp.raise_for_status()
    organic = resp.json().get("organic") or []
    if not organic:
        return None
    return [
        {"href": item.get("link", ""), "title": item.get("title", ""), "body": item.get("snippet", "")}
        for item in organic
    ]


def _gemini_search(query: str) -> list | None:
    """Use Gemini with Google Search grounding to run a search query.

    Returns the same `[{"href": ..., "title": ..., "body": ...}]` format as
    _ddg_text / _serper_text so the rest of the enricher stays backend-agnostic.
    Returns None for a genuine "no results". Raises RuntimeError when
    GEMINI_API_KEY is missing so the caller treats it as a permanent error.
    """
    try:
        import google.genai as genai  # type: ignore
        import google.genai.types as genai_types  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "google-genai package not installed. Run: pip install google-genai"
        ) from exc

    api_key = config.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set — required for the Gemini search backend."
        )

    client = genai.Client(api_key=api_key)
    prompt = (
        f"Search Google for: {query}\n\n"
        "Return the top search results as a JSON array (no markdown fences, "
        "no commentary — just the raw array):\n"
        '[{"href": "https://...", "title": "page title", "body": "short snippet"}, ...]\n\n'
        "Include up to 8 results. Only include entries with a valid https:// href. "
        "If genuinely no results found, return an empty array []."
    )

    max_retries = max(1, int(os.environ.get("GEMINI_MAX_RETRIES", "5")))

    def _is_gemini_transient(exc: Exception) -> bool:
        blob = str(exc).lower()
        return any(k in blob for k in ("429", "503", "resource_exhausted", "unavailable", "high demand", "rate"))

    response = None
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                ),
            )
            break
        except Exception as exc:
            if not _is_gemini_transient(exc) or attempt >= max_retries:
                raise
            delay = 30.0 * (attempt + 1) + random.uniform(1, 5)
            print(
                f"  [gemini] transient error ({exc!s:.80}) — waiting {delay:.0f}s "
                f"then retrying (attempt {attempt + 1}/{max_retries})…"
            )
            time.sleep(delay)

    raw_text = response.text if response is not None else None  # type: ignore[union-attr]
    if not raw_text:
        return None
    text = raw_text.strip()
    # Strip markdown fences if the model added them despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    parsed = json.loads(text)
    if not parsed:
        return None
    return [
        {
            "href":  str(r.get("href", "")),
            "title": str(r.get("title", "")),
            "body":  str(r.get("body", "")),
        }
        for r in parsed
        if r.get("href")
    ] or None


def _search_text(query: str) -> list | None:
    """Dispatch to the configured search backend (DDG, Serper, or Gemini)."""
    if config.SEARCH_BACKEND == "serper":
        return _serper_text(query)
    if config.SEARCH_BACKEND == "gemini":
        return _gemini_search(query)
    return _ddg_text(query)


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


def record_result(
    page_id: str,
    linkedin_url: str | None,
    *,
    existing_linkedin: str = "",
) -> None:
    """Persist one contact's outcome in a single Notion PATCH.

    Found  -> write the LinkedIn URL and stamp status "Yes".
    No match -> leave LinkedIn empty and stamp status "Skipped" (so it's never
    retried). Writing the URL stays type-aware (url or rich_text).

    If the row already has a LinkedIn URL, it is never overwritten — only the
    status stamp is updated when needed.
    """
    properties: dict = {}
    if existing_linkedin.strip():
        linkedin_url = existing_linkedin.strip()
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

    notion_sync.clear_schema_cache()

    print("Checking for contacts that already have LinkedIn (from scraper/manual)...")
    try:
        stamped = backfill_linkedin_status()
    except requests.RequestException as exc:
        # Non-fatal: the main empty-LinkedIn query still has client-side guards.
        print(f"[enricher] WARNING: could not backfill existing LinkedIn rows: {exc}")
        stamped = 0
    if stamped:
        print(
            f"  Stamped {stamped} row(s) as Yes — they already had LinkedIn and "
            "were not searched."
        )

    print("Finding contacts missing a LinkedIn profile...")
    try:
        contacts = fetch_contacts_missing_linkedin()
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"[enricher] could not query Notion: {exc}")
        return

    if not contacts:
        print("No contacts are missing a LinkedIn profile. Nothing to do.")
        return

    if config.SEARCH_BACKEND == "serper":
        backend_label = "Serper"
    elif config.SEARCH_BACKEND == "gemini":
        backend_label = "Gemini"
    else:
        backend_label = "DuckDuckGo"
    max_lookups = config.MAX_LOOKUPS  # may be overridden by interactive prompt
    if max_lookups == 0:
        print(
            f"Found {len(contacts)} contact(s) without LinkedIn. "
            f"No search cap — will process the full list via {backend_label}."
        )
    else:
        print(
            f"Found {len(contacts)} contact(s) without LinkedIn. "
            f"Will perform up to {max_lookups} {backend_label} searches this run."
        )

    lookups = 0  # actual search queries run (a contact can cost several)
    found = 0
    no_match = 0
    skipped = 0
    errors = 0
    consecutive_errors = 0  # drives the adaptive cooldown

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

        existing_linkedin = (contact.get("linkedin") or "").strip()
        if existing_linkedin:
            preview = (
                existing_linkedin
                if len(existing_linkedin) <= 60
                else existing_linkedin[:60] + "..."
            )
            print(f"  [skip ] '{name}' — LinkedIn already set ({preview}).")
            skipped += 1
            continue

        try:
            link, queries_run = search_linkedin(name, company)
            lookups += queries_run
            # DuckDuckGo answered cleanly — reset the back-off here (not after the
            # Notion write) so the streak reflects DDG health only, not Notion's.
            consecutive_errors = 0
            # Proactive burst break: pause every N queries to reset session
            # context. Only relevant for DDG (Serper disables this by default).
            if config.SEARCH_BACKEND == "serper":
                burst = config.SERPER_SEARCH_BURST_SIZE
                burst_break = config.SERPER_SEARCH_BURST_BREAK
            else:
                burst = config.SEARCH_BURST_SIZE
                burst_break = config.SEARCH_BURST_BREAK
            if burst > 0 and burst_break > 0 and lookups % burst == 0:
                print(
                    f"\n  [burst] {lookups} searches done — pausing "
                    f"{burst_break:.0f}s "
                    f"(proactive burst break for {backend_label})."
                )
                time.sleep(burst_break)
                print("  [burst] Resuming.\n")
            # Stamp the outcome either way: a verified URL -> "Yes"; no confident
            # match -> "Skipped" (LinkedIn stays empty, never retried).
            record_result(
                contact["page_id"],
                link,
                existing_linkedin=existing_linkedin,
            )
            if link:
                found += 1
                print(f"  [found] {name} ({company}) -> {link}")
            else:
                no_match += 1
                print(f"  [none ] {name} ({company}) — no match -> Skipped.")
        except requests.RequestException as exc:
            errors += 1
            detail = ""
            if exc.response is not None:
                detail = f" ({exc.response.status_code}: {exc.response.text[:200]})"
            print(f"  [warn ] '{name}' Notion write failed: {exc}{detail}")
        except Exception as exc:  # one bad contact must not abort the whole run
            if _is_transient_search_error(exc):
                errors += 1
                consecutive_errors += 1
                print(
                    f"  [warn ] '{name}' transient search error (will retry): {exc}"
                )
                _maybe_search_cooldown(consecutive_errors)
            else:
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
