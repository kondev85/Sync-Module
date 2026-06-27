"""Qualify IGB Live leads using DuckDuckGo context + Gemini AI.

For each Notion row where 'AI Evaluation' is blank:
  1. Grab Company Name + Role from the row.
  2. Group contacts by company — evaluate each company ONCE, then apply the
     same result to every person at that company (no duplicate API calls).
  3. Search DuckDuckGo for a short business-context snippet.
  4. Send that context to Gemini Flash with a strict system prompt.
  5. Gemini returns: company_type (5-8 word description), score (1-5), rationale (1 sentence).
  6. Write those three values back to Notion + stamp 'AI Evaluation = Done'.

All four AI columns are plain Text in Notion (rich_text), so no special
column types are required.

Run from the Shell (not the Agent sandbox — long runs need a real terminal):
  cd swapcard_sync && python -u company_evaluator.py

Env toggles:
  MAX_EVALUATIONS  — cap how many rows to process this run (0 = unlimited)
  EVAL_INTERVAL    — seconds between company evaluations (default 3.0)
  GEMINI_MODEL     — which Gemini model to use (default gemini-2.0-flash)
"""

import json
import os
import random
import re
import time

import requests
from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
from google import genai
from google.genai import types as genai_types

import config
import notion_sync

# ── Column names (must match Notion DB exactly — all are plain Text) ──────────
PROP_AI_EVAL      = "AI Evaluation"   # Text — gate column (see statuses below)
PROP_AI_TYPE      = "AI Category"     # Text — 5-8 word description of what the company does (reuses existing column)
PROP_AI_SCORE     = "AI Score"        # Text  (stored as "4/5" so it's human-readable)
PROP_AI_RATIONALE = "AI Rationale"    # Text
PROP_AI_CONTEXT   = "AI Web Context"  # Text — raw search snippet saved before Gemini call

PROP_WEBSITE_DOMAIN = "Website Domain"    # URL — official company website (e.g. https://kitsunestudios.co)
PROP_WEBSITE_EVAL   = "Website Evaluator" # Text — gate col; blank=pending, Done=written, Skipped=not found

# ── AI Evaluation status values ───────────────────────────────────────────────
# blank   → not yet processed (picked up on every run)
# "Done"  → successfully evaluated by Gemini
# "Skipped" → intentionally skipped (no company name)
# "Error" → Gemini failed; snippet saved; will be retried on next run
EVAL_STATUS_DONE    = "Done"
EVAL_STATUS_SKIPPED = "Skipped"
EVAL_STATUS_ERROR   = "Error"

# ── Website Evaluator status values ──────────────────────────────────────────
# blank     → not yet attempted
# "Done"    → domain written to Website Domain column
# "Skipped" → Gemini couldn't identify a confident match; Website Domain left empty
WEBSITE_STATUS_DONE    = "Done"
WEBSITE_STATUS_SKIPPED = "Skipped"

# ── Run settings ──────────────────────────────────────────────────────────────
MAX_EVALUATIONS = max(0, int(os.environ.get("MAX_EVALUATIONS", "0")))  # 0 = unlimited
EVAL_INTERVAL   = max(0.0, float(os.environ.get("EVAL_INTERVAL", "3.0")))
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

# ── Gemini system prompt ──────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a B2B lead qualifier for BlocksRace, a company that provides
innovative betting markets and live sports content to the iGaming industry.
BlocksRace sells its betting content/markets to casino operators, sportsbooks,
and iGaming platforms. It may also partner with tech companies or attract
crypto/VC investment.

Given a company name, the contact's role, and a short web search snippet about
the company, produce two things:

1. company_type — a precise 5-8 word description of exactly what this company does.
   Write it as a plain noun phrase, lowercase, no punctuation.
   Examples:
     "online casino and sportsbook operator"
     "iGaming affiliate network for casino traffic"
     "B2B payment processor for online gambling"
     "sports betting data API provider"
     "venture capital fund focused on crypto gaming"
     "SEO and content agency for iGaming brands"
     "white-label casino platform provider"
     "recruitment agency for gaming industry"
   Be specific — avoid vague terms like "technology company" or "service provider".
   Use the web context to identify their actual business function.

2. score — an integer 1-5 reflecting how valuable this company is to BlocksRace right now:
   5 = Casino Operator or Sportsbook actively needing new betting content
   4 = B2B iGaming tech platform with a clear integration angle (data feeds, platform APIs, etc.)
   3 = Crypto / VC with iGaming exposure or interest
   2 = Adjacent — could be useful as a service provider or partner in the future
   1 = No integration path with BlocksRace betting content — includes: payment processors,
       payment gateways, PSPs, fintech, affiliates, marketing agencies, SEO agencies,
       recruitment firms, regulators, compliance/certification bodies, media outlets.
       These companies provide services TO casinos but will never license betting markets.

   IMPORTANT: Payment processors and PSPs are ALWAYS score 1. BlocksRace sells betting
   content — it does not integrate with payment infrastructure. Do not score them above 1.

3. rationale — one concise sentence explaining the score.

4. website_domain — the company's official website domain.
   Inspect the Candidate URLs listed in the web context. Pick the one that is most
   likely the company's own homepage — exclude LinkedIn, Wikipedia, Crunchbase,
   Bloomberg, news sites, job boards, and directories.
   Return ONLY the bare domain (e.g. "kitsunestudios.co" or "www.example.com") —
   no https://, no trailing slash, no path.
   If none of the candidate URLs are clearly the official company site, return "".
   Do NOT invent a domain that is not in the provided candidate list.

Reply with ONLY a valid JSON object and nothing else — no markdown, no commentary:
{
  "company_type": "<5-8 word description of what the company does>",
  "score": <integer 1-5>,
  "rationale": "<one concise sentence explaining the score>",
  "website_domain": "<bare domain or empty string>"
}"""


def validate() -> None:
    """Ensure required secrets are present."""
    required = {
        "NOTION_API_TOKEN": config.NOTION_API_TOKEN,
        "NOTION_DATABASE_ID": config.NOTION_DATABASE_ID,
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing required environment secrets: "
            + ", ".join(missing)
            + ". Set them in Replit Secrets or a local .env file."
        )


# ── Notion helpers ────────────────────────────────────────────────────────────

def _plain_text(prop: dict | None) -> str:
    """Flatten a Notion title or rich_text property to a plain string."""
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _rich_text_value(prop: dict | None) -> str:
    """Read a rich_text column value as a plain string."""
    if not prop:
        return ""
    parts = prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _build_filter() -> dict:
    """Return rows where AI Evaluation is blank AND Company is not empty.

    Filtering out no-company rows server-side means they never enter the queue,
    the cap (MAX_EVALUATIONS) only counts actionable rows, and no Skipped stamps
    are written for contacts whose company is simply not filled in yet.
    """
    schema = notion_sync.get_schema()
    eval_type    = schema.get(PROP_AI_EVAL)
    company_type = schema.get(config.PROP_COMPANY)

    if eval_type == "rich_text":
        eval_empty = {"property": PROP_AI_EVAL, "rich_text": {"is_empty": True}}
    elif eval_type == "title":
        eval_empty = {"property": PROP_AI_EVAL, "title": {"is_empty": True}}
    else:
        raise RuntimeError(
            f"Property '{PROP_AI_EVAL}' not found or wrong type (got {eval_type!r}). "
            "Add a Text column named 'AI Evaluation' to your Notion database."
        )

    # Also pick up "Error" rows — Gemini failed last time, retry them.
    if eval_type == "rich_text":
        eval_error = {"property": PROP_AI_EVAL, "rich_text": {"equals": EVAL_STATUS_ERROR}}
    else:
        eval_error = {"property": PROP_AI_EVAL, "title": {"equals": EVAL_STATUS_ERROR}}

    needs_processing = {"or": [eval_empty, eval_error]}

    # Also require Company to be filled — no point evaluating a nameless company.
    if company_type in ("rich_text", "title"):
        company_not_empty = {
            "property": config.PROP_COMPANY,
            company_type: {"is_not_empty": True},
        }
        return {"and": [needs_processing, company_not_empty]}

    # Company column missing or unexpected type — fall back to eval-only filter
    return needs_processing


def fetch_needs_website() -> list[dict]:
    """Return rows where AI Evaluation=Done but Website Evaluator is blank.

    These are companies already scored by Gemini but not yet given a website —
    either they were processed before this feature existed, or the website pass
    failed previously. Rows stamped Done or Skipped in Website Evaluator are
    excluded so they are never retried automatically.
    """
    schema = notion_sync.get_schema()

    eval_type = schema.get(PROP_AI_EVAL)
    if eval_type == "rich_text":
        ai_done = {"property": PROP_AI_EVAL, "rich_text": {"equals": EVAL_STATUS_DONE}}
    elif eval_type == "title":
        ai_done = {"property": PROP_AI_EVAL, "title": {"equals": EVAL_STATUS_DONE}}
    else:
        raise RuntimeError(
            f"'{PROP_AI_EVAL}' column not found or wrong type. "
            "Run the AI Evaluator first so rows have 'Done' stamps."
        )

    web_eval_type = schema.get(PROP_WEBSITE_EVAL)
    if web_eval_type == "rich_text":
        web_empty = {"property": PROP_WEBSITE_EVAL, "rich_text": {"is_empty": True}}
    elif web_eval_type == "title":
        web_empty = {"property": PROP_WEBSITE_EVAL, "title": {"is_empty": True}}
    else:
        raise RuntimeError(
            f"'{PROP_WEBSITE_EVAL}' column not found (type={web_eval_type!r}). "
            "Create a Text column named 'Website Evaluator' in your Notion database."
        )

    company_type_str = schema.get(config.PROP_COMPANY)
    filters = [ai_done, web_empty]
    if company_type_str in ("rich_text", "title"):
        filters.append({
            "property": config.PROP_COMPANY,
            company_type_str: {"is_not_empty": True},
        })

    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    body_base = {"filter": {"and": filters}, "page_size": 100}
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
            contacts.append({
                "page_id":        page["id"],
                "name":           _plain_text(props.get(config.PROP_NAME)),
                "company":        _plain_text(props.get(config.PROP_COMPANY)),
                "role":           _plain_text(props.get(config.PROP_ROLE)),
                "cached_snippet": _rich_text_value(props.get(PROP_AI_CONTEXT)),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return contacts


def fetch_unevaluated() -> list[dict]:
    """Page through the DB and return all rows that need evaluation.

    Includes blank AI Evaluation rows AND rows stamped 'Error' (Gemini failed
    last time — retry them). Also reads any cached AI Web Context snippet so
    Error retries skip the DuckDuckGo search when a snippet is already saved.
    """
    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    body_base = {"filter": _build_filter(), "page_size": 100}
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
            contacts.append({
                "page_id":        page["id"],
                "name":           _plain_text(props.get(config.PROP_NAME)),
                "company":        _plain_text(props.get(config.PROP_COMPANY)),
                "role":           _plain_text(props.get(config.PROP_ROLE)),
                "cached_snippet": _rich_text_value(props.get(PROP_AI_CONTEXT)),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return contacts


def _rt(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}

def _title_rt(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}

def _patch_page(page_id: str, properties: dict) -> None:
    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    resp = notion_sync._notion_request("PATCH", url, {"properties": properties})
    resp.raise_for_status()


def _write_snippet(page_id: str, snippet: str) -> None:
    """Save the raw search snippet to Notion BEFORE calling Gemini.

    Persisting the snippet early means it survives a Gemini error — a human
    can read it to make a manual judgment, and the next automated retry can
    use it without a repeat DuckDuckGo search.
    """
    schema = notion_sync.get_schema()
    ptype = schema.get(PROP_AI_CONTEXT)
    if ptype not in ("rich_text", "title"):
        return  # column not created yet — silently skip
    prop_value = _rt(snippet) if ptype == "rich_text" else _title_rt(snippet)
    _patch_page(page_id, {PROP_AI_CONTEXT: prop_value})


def _write_website(page_id: str, domain: str, status: str) -> None:
    """Write the website domain (URL column) and Website Evaluator stamp.

    `domain` should be a bare domain like "kitsunestudios.co" or "" when
    Gemini couldn't identify a confident match. The stamp is always written
    so the row is not picked up again on the next run.
    """
    schema = notion_sync.get_schema()
    properties: dict = {}

    if domain:
        url_type = schema.get(PROP_WEBSITE_DOMAIN)
        if url_type == "url":
            clean = domain.rstrip("/").strip()
            full_url = clean if clean.startswith("http") else f"https://{clean}"
            properties[PROP_WEBSITE_DOMAIN] = {"url": full_url}
        else:
            print(
                f"  [website] WARNING: '{PROP_WEBSITE_DOMAIN}' column not found or wrong type "
                f"(got {url_type!r}). URL '{domain}' will NOT be saved. "
                f"Check the column name in Notion matches exactly: '{PROP_WEBSITE_DOMAIN}' (type URL)."
            )

    web_eval_type = schema.get(PROP_WEBSITE_EVAL)
    if web_eval_type == "rich_text":
        properties[PROP_WEBSITE_EVAL] = _rt(status)
    elif web_eval_type == "title":
        properties[PROP_WEBSITE_EVAL] = _title_rt(status)
    else:
        print(
            f"  [website] WARNING: '{PROP_WEBSITE_EVAL}' column not found "
            f"(type={web_eval_type!r}). Create a Text column with that exact name."
        )

    if properties:
        _patch_page(page_id, properties)


def _write_result(
    page_id: str,
    company_type: str,
    score: int,
    rationale: str,
    status: str,
    snippet: str = "",
) -> None:
    """Patch all AI columns onto a Notion row.

    `snippet` is written to AI Web Context only when non-empty and the column
    exists — callers that already saved the snippet via _write_snippet can
    omit it here to avoid a redundant write.
    """
    schema = notion_sync.get_schema()
    properties: dict = {}

    pairs = [
        (PROP_AI_TYPE,      company_type),
        (PROP_AI_SCORE,     f"{score}/5"),
        (PROP_AI_RATIONALE, rationale),
        (PROP_AI_EVAL,      status),
    ]
    if snippet:
        pairs.append((PROP_AI_CONTEXT, snippet))

    for prop, value in pairs:
        ptype = schema.get(prop)
        if ptype == "rich_text":
            properties[prop] = _rt(value)
        elif ptype == "title":
            properties[prop] = _title_rt(value)
        else:
            # Missing column → silently skip non-critical columns, but warn loudly
            # if it's the gate column — a missing AI Evaluation stamp means the row
            # will be re-fetched and re-evaluated on every future run.
            if prop == PROP_AI_EVAL:
                print(
                    f"  [eval ] ERROR: '{PROP_AI_EVAL}' column not found in schema "
                    f"(type={ptype!r}). 'Done' stamp will NOT be written — this row "
                    f"will be re-evaluated on the next run. Check the column name."
                )

    if not properties:
        print(f"  [eval ] WARNING: no writable AI columns found for page {page_id}.")
        return

    _patch_page(page_id, properties)


# ── Web search for company context ───────────────────────────────────────────

def _results_to_snippet(results: list) -> str:
    """Flatten a list of search result dicts into a single ≤800-char snippet."""
    parts = []
    for r in results[:3]:
        title = r.get("title", "")
        body  = r.get("body", "") or r.get("snippet", "")
        if title:
            parts.append(title)
        if body:
            parts.append(body)
    return " | ".join(parts)[:800]


def _results_to_urls(results: list) -> list[str]:
    """Extract candidate page URLs from search results (up to 5).

    These are passed to Gemini so it can identify the company's official homepage
    rather than inferring it from snippet text alone.
    """
    urls = []
    for r in results[:5]:
        href = r.get("href") or r.get("link") or r.get("url") or ""
        if href and href.startswith("http"):
            urls.append(href)
    return urls


def _ddg_search(query: str) -> list[dict]:
    """Try one DuckDuckGo query; return raw result list (empty on failure)."""
    try:
        with DDGS(timeout=20) as ddgs:
            return ddgs.text(query, max_results=5) or []
    except (RatelimitException, TimeoutException, DDGSException, Exception):
        return []


def _serper_search(query: str) -> list[dict]:
    """Try one Serper.dev query; return normalised result list (empty on failure).

    Only attempted when SERPER_API_KEY is set — returns [] otherwise.
    """
    if not config.SERPER_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=20,
        )
        resp.raise_for_status()
        organic = resp.json().get("organic") or []
        return [
            {
                "title": r.get("title", ""),
                "body": r.get("snippet", ""),
                "href": r.get("link", ""),
            }
            for r in organic
        ]
    except Exception:
        return []


def _snippet_mentions_company(snippet: str, company: str) -> bool:
    """Return True if at least one meaningful token from the company name
    appears in the snippet — guards against DuckDuckGo returning a popular
    iGaming brand (e.g. GLI) for unrelated company searches.

    Strips legal suffixes and short noise words before checking so that
    "KA Gaming" matches on "KA" and "Gaming", etc.
    """
    noise = {"the", "and", "of", "for", "in", "a", "an", "ltd", "limited",
             "inc", "llc", "group", "holdings", "international", "intl", "co"}
    tokens = [
        t for t in re.sub(r"[^\w\s]", " ", company.lower()).split()
        if len(t) > 1 and t not in noise
    ]
    if not tokens:
        return True   # nothing meaningful to check — don't discard
    snippet_lower = snippet.lower()
    # Require at least one token (2+ chars) to appear verbatim
    return any(tok in snippet_lower for tok in tokens)


def _search_company(company: str) -> tuple[str, list[str]]:
    """Return (snippet, candidate_urls) for a company name.

    Strategy (most to least reliable):
      1. DuckDuckGo — iGaming-flavoured query (best signal for our use-case)
      2. DuckDuckGo — plain name-only fallback
      3. Serper.dev — iGaming query (if SERPER_API_KEY is set)
      4. Serper.dev — plain name-only (if SERPER_API_KEY is set)

    Each snippet is validated: if the company name doesn't appear in the
    result, the snippet is discarded. candidate_urls are the raw hrefs from
    the winning result set — passed to Gemini for website domain identification.

    Returns ("", []) if every attempt fails.
    """
    igaming_query = f'"{company}" casino OR sportsbook OR betting OR iGaming'
    plain_query   = f'"{company}"'

    # ── DuckDuckGo first (free) ──────────────────────────────────────────────
    for query in (igaming_query, plain_query):
        results = _ddg_search(query)
        if results:
            snippet = _results_to_snippet(results)
            if snippet and _snippet_mentions_company(snippet, company):
                return snippet, _results_to_urls(results)
            if snippet:
                print(f"  [search] Snippet discarded — doesn't mention '{company}'. Trying next query.")
        time.sleep(1)   # brief pause between DDG attempts

    # ── Serper fallback (paid, if key is set) ────────────────────────────────
    if config.SERPER_API_KEY:
        for query in (igaming_query, plain_query):
            results = _serper_search(query)
            if results:
                snippet = _results_to_snippet(results)
                if snippet and _snippet_mentions_company(snippet, company):
                    print("  [search] DDG failed — used Serper fallback.")
                    return snippet, _results_to_urls(results)
                if snippet:
                    print(f"  [search] Serper snippet discarded — doesn't mention '{company}'.")
            time.sleep(0.5)

    print(f"  [search] No valid snippet found for '{company}' — Gemini will assess by name only.")
    return "", []


# ── Gemini AI assessment ──────────────────────────────────────────────────────

# Max retries when Gemini returns a 429 (rate limit). The script reads the
# retryDelay from the error body and waits exactly that long before retrying.
GEMINI_MAX_RETRIES = max(1, int(os.environ.get("GEMINI_MAX_RETRIES", "5")))


def _gemini_client() -> genai.Client:
    """Create a Gemini client always using GEMINI_API_KEY.

    The google-genai SDK prefers GOOGLE_API_KEY over GEMINI_API_KEY when both
    env vars exist, which can route calls to the wrong project/quota. We
    temporarily hide GOOGLE_API_KEY so the SDK uses our explicit key.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    shadowed = os.environ.pop("GOOGLE_API_KEY", None)
    try:
        client = genai.Client(api_key=api_key)
    finally:
        if shadowed is not None:
            os.environ["GOOGLE_API_KEY"] = shadowed
    return client


def _parse_retry_delay(exc: Exception) -> float | None:
    """Extract the retryDelay seconds from a Gemini 429 error, if present.

    The google-genai SDK wraps the API error as an Exception whose str()
    contains the raw JSON body. We look for 'retryDelay': '21s' or similar.
    Returns None when no delay can be found (caller falls back to a default).
    """
    blob = str(exc)
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?\s*(\d+(?:\.\d+)?)\s*s", blob)
    if match:
        return float(match.group(1))
    # Also handle plain integer seconds in the message text
    match = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", blob, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def _is_gemini_rate_limit(exc: Exception) -> bool:
    """True when the exception is a transient Gemini error worth retrying.

    Covers 429 RESOURCE_EXHAUSTED (quota) and 503 UNAVAILABLE (high demand).
    """
    blob = str(exc).lower()
    return any(k in blob for k in ("429", "503", "resource_exhausted", "unavailable", "high demand", "rate"))


def _call_gemini(client: genai.Client, user_message: str) -> str:
    """Call Gemini with automatic retry on 429, honouring the retryDelay.

    Waits exactly as long as Gemini asks (retryDelay in the error body),
    falling back to an escalating default if no delay is specified.
    Raises the last exception after GEMINI_MAX_RETRIES attempts.
    """
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_message,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    temperature=0.1,
                    max_output_tokens=1024,
                    response_mime_type="application/json",
                ),
            )
            return (response.text or "").strip()
        except Exception as exc:
            if not _is_gemini_rate_limit(exc) or attempt >= GEMINI_MAX_RETRIES:
                raise
            delay = _parse_retry_delay(exc)
            if delay is None:
                delay = 30.0 * (attempt + 1)   # escalating fallback: 30s, 60s, 90s…
            # Add a small jitter so parallel runs don't all retry in sync
            delay += random.uniform(1, 5)
            print(
                f"  [gemini] 429 rate limit — waiting {delay:.0f}s then retrying "
                f"(attempt {attempt + 1}/{GEMINI_MAX_RETRIES})…"
            )
            time.sleep(delay)
    raise RuntimeError("Gemini retry limit exceeded")  # unreachable but makes mypy happy


def assess_company(
    client: genai.Client,
    company: str,
    role: str,
    snippet: str,
    candidate_urls: list[str] | None = None,
) -> dict:
    """Send company info to Gemini and parse the JSON response.

    Returns a dict with keys: company_type, score, rationale, website_domain.
    Raises on unrecoverable errors (caller decides whether to stamp Skipped).
    """
    url_line = ""
    if candidate_urls:
        url_line = "\nCandidate URLs: " + ", ".join(candidate_urls[:5])

    user_message = (
        f"Company: {company or '(unknown)'}\n"
        f"Contact role: {role or '(unknown)'}\n"
        f"Web context: {snippet or '(no search results found)'}"
        f"{url_line}"
    )
    raw = _call_gemini(client, user_message)

    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned non-JSON: {raw[:300]}") from exc

    company_type   = str(result.get("company_type", "")).strip()
    score_raw      = result.get("score", 1)
    rationale      = str(result.get("rationale", "")).strip()
    website_domain = str(result.get("website_domain", "")).strip()

    if not company_type:
        company_type = "unknown business type"

    try:
        score = max(1, min(5, int(score_raw)))
    except (TypeError, ValueError):
        score = 1

    if not rationale:
        rationale = "No rationale provided."

    return {
        "company_type": company_type,
        "score": score,
        "rationale": rationale,
        "website_domain": website_domain,
    }


# ── Website-only Gemini assessment (for the standalone website pass) ──────────

_WEBSITE_SYSTEM_PROMPT = """You are identifying a company's official website.

Given a company name and a list of candidate URLs found in web search results,
return the URL that is most likely the company's official homepage.

Exclude: LinkedIn, Wikipedia, Crunchbase, Bloomberg, news articles, job boards,
app stores, industry directories, and any URL that is clearly not the company's
own website.

Return ONLY a valid JSON object — no markdown, no commentary:
{
  "website_domain": "<bare domain e.g. kitsunestudios.co or www.example.com>"
}

If none of the candidate URLs are clearly the official company website, return:
{
  "website_domain": ""
}

Do NOT invent or guess a domain that is not in the provided list."""


def find_website_domain(
    client: genai.Client,
    company: str,
    snippet: str,
    candidate_urls: list[str],
) -> str:
    """Ask Gemini (website-only prompt) to pick the best domain from candidate URLs.

    Returns a bare domain string (e.g. "kitsunestudios.co") or "" if Gemini
    cannot identify a confident match. Uses a minimal prompt — no company
    type/score/rationale — to keep cost low for the standalone website pass.
    """
    if not candidate_urls:
        return ""

    user_message = (
        f"Company: {company or '(unknown)'}\n"
        f"Web context: {snippet or '(no search results found)'}\n"
        f"Candidate URLs: {', '.join(candidate_urls[:5])}"
    )

    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_message,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_WEBSITE_SYSTEM_PROMPT,
                    temperature=0.1,
                    max_output_tokens=128,
                    response_mime_type="application/json",
                ),
            )
            raw = (response.text or "").strip()
            raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```$", "", raw)
            result = json.loads(raw)
            return str(result.get("website_domain", "")).strip()
        except Exception as exc:
            if not _is_gemini_rate_limit(exc) or attempt >= GEMINI_MAX_RETRIES:
                print(f"  [website] Gemini error: {exc}")
                return ""
            delay = (_parse_retry_delay(exc) or 30.0 * (attempt + 1)) + random.uniform(1, 5)
            print(f"  [website] 429 rate limit — waiting {delay:.0f}s (attempt {attempt + 1})…")
            time.sleep(delay)
    return ""


# ── Company-level deduplication (fuzzy name matching) ────────────────────────

# Legal / generic suffixes stripped before comparing company names.
# These carry no identity signal and differ freely across data entry styles.
_LEGAL_SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "lp",
    "plc", "gmbh", "ag", "sa", "srl", "bv", "oy", "ab", "as", "kft",
    "corp", "corporation", "co",
    "group", "groups", "holding", "holdings",
    "international", "intl",
    "the",
}


def _canonical_tokens(name: str) -> frozenset[str]:
    """Return a frozenset of normalised, suffix-stripped tokens for a company name.

    Steps:
      1. Lowercase and replace punctuation with spaces.
      2. Split into words; drop very short noise tokens (1 char).
      3. Remove legal/generic suffixes (ltd, group, holdings, …).
      4. Return as frozenset for fast subset checks.

    Examples:
      "FDJ United"  → {"fdj", "united"}
      "FDJ UNITED"  → {"fdj", "united"}   ← same as above
      "FDJ"         → {"fdj"}             ← subset of above
      "Company Ltd" → {"company"}
      "Company"     → {"company"}         ← same canonical key
    """
    clean = re.sub(r"[^\w\s]", " ", (name or "").lower())
    tokens = [t for t in clean.split() if len(t) > 1 and t not in _LEGAL_SUFFIXES]
    return frozenset(tokens)


def _companies_match(a: frozenset, b: frozenset) -> bool:
    """True when two companies should be treated as the same entity.

    Rules (conservative — prefer false negatives over false positives):
    1. Identical token sets → always match.
       ("FDJ United" == "FDJ UNITED" after normalisation.)
    2. Strict subset → match ONLY when:
       a) The shorter set has ≥ 1 token of at least 3 chars (so "co" alone
          never merges unrelated companies), AND
       b) The longer set has at most 2 more tokens than the shorter
          (keeps "FDJ" merging into "FDJ United" but stops "FDJ" accidentally
          merging with "FDJ United Kingdom Lottery Division International").

    Counter-example that is safely rejected:
      "Scientific Games"   → {"scientific", "games"}
      "Scientific Industries" → {"scientific", "industries"}
      Neither is a subset of the other → NOT merged. ✓
    """
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if not shorter.issubset(longer):
        return False
    # Safety guard 1: require at least one substantive token (≥3 chars)
    if not any(len(t) >= 3 for t in shorter):
        return False
    # Safety guard 2: cap how many extra tokens the longer name may have
    if len(longer) - len(shorter) > 2:
        return False
    return True


def _group_by_company(contacts: list[dict]) -> dict[str, list[dict]]:
    """Group contacts into clusters where all members share the same company.

    Uses a union-find over canonical token sets to merge entries that refer to
    the same company despite variations in capitalisation, punctuation, and
    legal suffixes (e.g. "FDJ" / "FDJ United" / "FDJ UNITED" / "Company Ltd" /
    "Company").

    Contacts with no company name are each placed in their own singleton group
    (keyed by page_id) so they are still evaluated individually.
    """
    # ── Assign each contact a canonical token set ──────────────────────────
    labelled: list[tuple[str, frozenset, dict]] = []  # (raw_company, tokens, contact)
    for c in contacts:
        raw = (c["company"] or "").strip()
        if raw:
            labelled.append((raw, _canonical_tokens(raw), c))
        else:
            # No company — singleton keyed by page_id, evaluated alone
            labelled.append(("", frozenset(), c))

    # ── Union-find ─────────────────────────────────────────────────────────
    n = len(labelled)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            _, ti, _ = labelled[i]
            _, tj, _ = labelled[j]
            if ti and tj and _companies_match(ti, tj):
                union(i, j)

    # ── Build groups keyed by the longest company name in each cluster ─────
    # (Longest name is most descriptive — use it as the display label.)
    clusters: dict[int, list[tuple[str, dict]]] = {}
    for idx, (raw, _, contact) in enumerate(labelled):
        root = find(idx)
        clusters.setdefault(root, []).append((raw, contact))

    groups: dict[str, list[dict]] = {}
    for members in clusters.values():
        # Pick the longest original name as the representative key
        rep_name = max((raw for raw, _ in members if raw), key=len, default="")
        if not rep_name:
            # All members had no company — give each its own singleton group
            for _, contact in members:
                groups[f"__no_company_{contact['page_id']}"] = [contact]
            continue
        groups[rep_name] = [contact for _, contact in members]

    return groups


# ── Main run loop ─────────────────────────────────────────────────────────────

def run() -> None:
    validate()
    notion_sync.get_schema()  # warm the cache + validate DB is reachable

    print("\n=== Company Evaluator (BlocksRace lead qualifier) ===")
    print(f"Model : {GEMINI_MODEL}")
    print(f"Pacing: {EVAL_INTERVAL}s between company evaluations")
    if MAX_EVALUATIONS:
        print(f"Cap   : {MAX_EVALUATIONS} rows this run")
    print()

    # ── Schema check ──────────────────────────────────────────────────────────
    schema = notion_sync.get_schema()
    missing_cols = []
    for col in [PROP_AI_EVAL, PROP_AI_TYPE, PROP_AI_SCORE, PROP_AI_RATIONALE]:
        if schema.get(col) not in ("rich_text", "title", "text"):
            missing_cols.append(
                f"  '{col}' (type is {schema.get(col)!r} — needs to be a Text column)"
            )
    if schema.get(PROP_WEBSITE_DOMAIN) != "url":
        missing_cols.append(
            f"  '{PROP_WEBSITE_DOMAIN}' (type is {schema.get(PROP_WEBSITE_DOMAIN)!r} "
            f"— needs to be a URL column; website domains will be SKIPPED until fixed)"
        )
    if missing_cols:
        print("WARNING — the following Notion columns are missing or wrong type:")
        for m in missing_cols:
            print(m)
        print()

    print("Fetching unevaluated rows from Notion…")
    contacts = fetch_unevaluated()
    total_rows = len(contacts)
    print(f"Found {total_rows} unevaluated rows.")

    if not contacts:
        print("Nothing to do — all rows already have an AI Evaluation stamp.")
        return

    # Group by company — evaluate each company once, stamp all its contacts
    groups = _group_by_company(contacts)
    total_companies = len(groups)
    print(f"Unique companies to evaluate: {total_companies}")
    print(
        f"({total_rows - total_companies} rows will be stamped instantly "
        f"from cached results, saving ~{total_rows - total_companies} API calls)\n"
        if total_rows > total_companies else "\n"
    )

    if MAX_EVALUATIONS:
        # Cap applies to rows processed, not companies evaluated
        all_contacts_ordered = [c for group in groups.values() for c in group]
        capped = all_contacts_ordered[:MAX_EVALUATIONS]
        # Rebuild groups from the capped set
        groups = _group_by_company(capped)
        print(f"Capped to first {len(capped)} rows ({len(groups)} companies).\n")

    client = _gemini_client()

    done = stamped_from_cache = skipped = errors = 0
    company_num = 0

    for key, group in groups.items():
        company_num += 1
        # Use the first contact's company name as display label (original case)
        company = group[0]["company"] or ""
        role    = group[0]["role"]  # role of the first contact (for context)

        # Skip rows with no company — evaluating a person's name as a company
        # is meaningless and wastes API quota. Stamp them Skipped so they
        # don't re-appear on every run (a human can clear the stamp if the
        # company is later filled in).
        if not company:
            names = ", ".join(c["name"] or "(no name)" for c in group)
            print(f"[company {company_num}/{len(groups)}] (no company name) → {names}")
            print("  Skipping — company field is empty.")
            for contact in group:
                try:
                    _write_result(contact["page_id"], "no company name provided", 1,
                                  "No company name — cannot evaluate.", "Skipped")
                except Exception:
                    pass
            skipped += len(group)
            continue

        n_contacts = len(group)
        label = f"[company {company_num}/{len(groups)}] {company}"
        if n_contacts > 1:
            label += f"  ({n_contacts} contacts)"

        print(label)

        # ── Web search snippet + candidate URLs (DDG → Serper fallback) ────
        # Use the cached snippet from Notion if it actually mentions this company.
        # Validate even cached snippets — earlier runs may have saved contaminated
        # results (e.g. GLI content returned for unrelated company searches).
        # Note: cached snippets don't contain URLs, so we always re-search for
        # candidate_urls when the snippet is fresh; for cached hits we pass [] and
        # Gemini does its best from the company name alone.
        cached = group[0].get("cached_snippet", "")
        candidate_urls: list[str] = []
        if cached and _snippet_mentions_company(cached, company):
            snippet = cached
            print(f"  Snippet : {snippet[:80]}{'…' if len(snippet) > 80 else ''} [cached]")
        else:
            if cached:
                print(f"  Snippet : cached snippet invalid for '{company}' — fetching fresh.")
            snippet, candidate_urls = _search_company(company)
            print(f"  Snippet : {snippet[:80]}{'…' if len(snippet) > 80 else ''}")
            if candidate_urls:
                print(f"  URLs    : {', '.join(candidate_urls[:3])}")
            # ── Save snippet to ALL contacts in this group BEFORE calling Gemini ──
            # This ensures the context is in Notion even if Gemini fails below.
            for contact in group:
                try:
                    _write_snippet(contact["page_id"], snippet)
                except Exception:
                    pass

        # ── Gemini assessment ───────────────────────────────────────────────
        try:
            result = assess_company(client, company, role, snippet, candidate_urls)
        except Exception as exc:
            print(f"  [ERROR] Gemini failed: {exc}")
            errors += len(group)
            # Stamp "Error" (NOT Skipped) — this row will be retried on the
            # next run. The snippet is already saved so no DDG call needed.
            # We do NOT write a fake category/score so a human reading the
            # row sees blank fields + the web context snippet for judgment.
            for contact in group:
                try:
                    schema = notion_sync.get_schema()
                    ptype = schema.get(PROP_AI_EVAL)
                    if ptype == "rich_text":
                        _patch_page(contact["page_id"],
                                    {PROP_AI_EVAL: _rt(EVAL_STATUS_ERROR)})
                    elif ptype == "title":
                        _patch_page(contact["page_id"],
                                    {PROP_AI_EVAL: _title_rt(EVAL_STATUS_ERROR)})
                except Exception:
                    pass
            _pace(company_num, len(groups))
            continue

        company_type    = result["company_type"]
        score           = result["score"]
        rat             = result["rationale"]
        website_domain  = result.get("website_domain", "")
        if website_domain:
            print(f"  → {company_type}  |  Score {score}/5  |  {rat}  |  🌐 {website_domain}")
        else:
            print(f"  → {company_type}  |  Score {score}/5  |  {rat}  |  (no website found)")

        # ── Write to all contacts in this group ─────────────────────────────
        first = True
        for contact in group:
            try:
                _write_result(contact["page_id"], company_type, score, rat, EVAL_STATUS_DONE)
                # Write website domain + evaluator stamp for every contact in group
                try:
                    web_status = WEBSITE_STATUS_DONE if website_domain else WEBSITE_STATUS_SKIPPED
                    _write_website(contact["page_id"], website_domain, web_status)
                except Exception as web_exc:
                    print(f"  [website] Write failed for {contact['name']!r}: {web_exc}")
                done += 1
                if not first:
                    stamped_from_cache += 1
                    print(
                        f"    ✓ {contact['name'] or '(no name)'} "
                        f"— stamped from company cache (no extra API call)"
                    )
            except requests.RequestException as exc:
                print(
                    f"  [ERROR] Notion write failed for "
                    f"{contact['name']!r}: {exc}"
                )
                errors += 1
            first = False

        _pace(company_num, len(groups))

    print(
        f"\n✓ Finished.  "
        f"Rows written: {done}  "
        f"(of which {stamped_from_cache} were duplicate-company rows, "
        f"zero extra API calls)  "
        f"Errors: {errors}"
    )


def _pace(index: int, total: int) -> None:
    """Sleep between company evaluations; skip the pause after the last one."""
    if index < total and EVAL_INTERVAL > 0:
        jitter = random.uniform(-0.3, 0.3) * EVAL_INTERVAL
        time.sleep(max(0.0, EVAL_INTERVAL + jitter))


def run_website_pass() -> None:
    """Standalone pass: find website domains for already-evaluated companies.

    Picks up rows where AI Evaluation=Done but Website Evaluator is blank.
    For each unique company: re-searches DDG for candidate URLs, then calls
    Gemini (website-only prompt) to pick the official homepage.

    Stamps every contact in the company group with Done or Skipped in
    Website Evaluator so they are never retried automatically.
    """
    validate()
    notion_sync.get_schema()

    print("\n=== Website Domain Finder (already-evaluated companies) ===")
    print(f"Model : {GEMINI_MODEL}")
    print(f"Pacing: {EVAL_INTERVAL}s between company lookups")
    if MAX_EVALUATIONS:
        print(f"Cap   : {MAX_EVALUATIONS} rows this run")
    print()

    schema = notion_sync.get_schema()
    web_eval_type = schema.get(PROP_WEBSITE_EVAL)
    if web_eval_type not in ("rich_text", "title"):
        print(
            f"ERROR: '{PROP_WEBSITE_EVAL}' column not found or wrong type "
            f"(got {web_eval_type!r}).\n"
            "Create a Text column named 'Website Evaluator' in your Notion database."
        )
        return

    print("Fetching rows that need a website lookup…")
    try:
        contacts = fetch_needs_website()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return

    total_rows = len(contacts)
    print(f"Found {total_rows} rows needing website lookup.")

    if not contacts:
        print("Nothing to do — all evaluated rows already have a Website Evaluator stamp.")
        return

    groups = _group_by_company(contacts)
    total_companies = len(groups)
    print(f"Unique companies: {total_companies}\n")

    if MAX_EVALUATIONS:
        all_ordered = [c for group in groups.values() for c in group]
        capped = all_ordered[:MAX_EVALUATIONS]
        groups = _group_by_company(capped)
        print(f"Capped to first {len(capped)} rows ({len(groups)} companies).\n")

    client = _gemini_client()
    done = skipped = errors = 0
    company_num = 0

    for key, group in groups.items():
        company_num += 1
        company = group[0]["company"] or ""

        if not company:
            for contact in group:
                try:
                    _write_website(contact["page_id"], "", WEBSITE_STATUS_SKIPPED)
                except Exception:
                    pass
            skipped += len(group)
            continue

        n_contacts = len(group)
        label = f"[{company_num}/{len(groups)}] {company}"
        if n_contacts > 1:
            label += f"  ({n_contacts} contacts)"
        print(label)

        # Search for candidate URLs — reuse cached snippet if valid, re-search if not
        cached = group[0].get("cached_snippet", "")
        if cached and _snippet_mentions_company(cached, company):
            snippet = cached
            print(f"  Snippet : [cached]  Searching URLs fresh…")
            # Cached snippet = text only, no URLs stored; do a quick plain search
            results = _ddg_search(f'"{company}"') or []
            if not results and config.SERPER_API_KEY:
                results = _serper_search(f'"{company}"') or []
            candidate_urls = _results_to_urls(results)
        else:
            snippet, candidate_urls = _search_company(company)

        if candidate_urls:
            print(f"  URLs    : {', '.join(candidate_urls[:3])}")
        else:
            print(f"  URLs    : none found — will stamp Skipped")

        # Call Gemini website-only prompt
        domain = find_website_domain(client, company, snippet, candidate_urls)

        if domain:
            print(f"  → 🌐 {domain}")
            web_status = WEBSITE_STATUS_DONE
        else:
            print(f"  → Skipped (no confident match)")
            web_status = WEBSITE_STATUS_SKIPPED

        for contact in group:
            try:
                _write_website(contact["page_id"], domain, web_status)
                if domain:
                    done += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"  [ERROR] Notion write failed for {contact['name']!r}: {exc}")
                errors += 1

        _pace(company_num, len(groups))

    print(
        f"\n✓ Finished.  "
        f"Domains written: {done}  Skipped: {skipped}  Errors: {errors}"
    )


def reset_gemini_errors() -> None:
    """Clear rows incorrectly stamped 'Skipped' due to a Gemini error.

    Earlier script versions stamped Gemini failures as 'Skipped' with fake
    category 'Unrelated / 1/5'. This function finds those rows (identified
    by the 'Gemini error' text in AI Rationale) and resets them to blank so
    the next normal run picks them up and evaluates them properly.

    Run once from the Shell to fix the bad data:
      cd swapcard_sync && python -c "import company_evaluator; company_evaluator.reset_gemini_errors()"
    """
    import config as _config
    import notion_sync as _ns

    _ns.get_schema()
    schema = _ns.get_schema()
    rat_type = schema.get(PROP_AI_RATIONALE)
    if rat_type not in ("rich_text", "title"):
        print("AI Rationale column not found — nothing to reset.")
        return

    url = f"{_config.NOTION_API_URL}/databases/{_config.NOTION_DATABASE_ID}/query"
    # Find rows where AI Rationale contains "Gemini error"
    body_base = {
        "filter": {
            "property": PROP_AI_RATIONALE,
            rat_type: {"contains": "Gemini error"},
        },
        "page_size": 100,
    }

    reset_count = 0
    cursor: str | None = None
    print("Scanning for rows with 'Gemini error' in AI Rationale…")
    while True:
        body = dict(body_base)
        if cursor:
            body["start_cursor"] = cursor
        resp = _ns._notion_request("POST", url, body)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            page_id = page["id"]
            props   = page.get("properties", {})
            company = _plain_text(props.get(_config.PROP_COMPANY)) or "(no company)"
            # Clear AI Evaluation, AI Company Type, AI Score, AI Rationale
            clear: dict = {}
            for prop in (PROP_AI_EVAL, PROP_AI_TYPE, PROP_AI_RATIONALE):
                ptype = schema.get(prop)
                if ptype == "rich_text":
                    clear[prop] = {"rich_text": []}
                elif ptype == "title":
                    clear[prop] = {"title": []}
            if schema.get(PROP_AI_SCORE) == "rich_text":
                clear[PROP_AI_SCORE] = {"rich_text": []}
            if clear:
                try:
                    _patch_page(page_id, clear)
                    reset_count += 1
                    print(f"  ✓ Reset: {company}")
                except Exception as exc:
                    print(f"  ✗ Failed to reset {company}: {exc}")
            time.sleep(0.3)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break

    print(f"\nDone. {reset_count} rows reset to blank — they will be re-evaluated on the next run.")


if __name__ == "__main__":
    run()
