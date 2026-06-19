"""Qualify IGB Live leads using DuckDuckGo context + Gemini AI.

For each Notion row where 'AI Evaluation' is blank:
  1. Grab Company Name + Role from the row.
  2. Group contacts by company — evaluate each company ONCE, then apply the
     same result to every person at that company (no duplicate API calls).
  3. Search DuckDuckGo for a short business-context snippet.
  4. Send that context to Gemini Flash with a strict system prompt.
  5. Gemini returns: category, score (1-5), rationale (1 sentence).
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
PROP_AI_EVAL      = "AI Evaluation"   # Text — blank = unprocessed, "Done"/"Skipped" = done
PROP_AI_CATEGORY  = "AI Category"     # Text
PROP_AI_SCORE     = "AI Score"        # Text  (stored as "4/5" so it's human-readable)
PROP_AI_RATIONALE = "AI Rationale"    # Text

# ── Run settings ──────────────────────────────────────────────────────────────
MAX_EVALUATIONS = max(0, int(os.environ.get("MAX_EVALUATIONS", "0")))  # 0 = unlimited
EVAL_INTERVAL   = max(0.0, float(os.environ.get("EVAL_INTERVAL", "3.0")))
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# ── Category labels ───────────────────────────────────────────────────────────
CATEGORY_CASINO    = "Casino Operator"
CATEGORY_CRYPTO    = "Crypto / VC"
CATEGORY_TECH      = "Strategic Tech Partner"
CATEGORY_UNRELATED = "Unrelated"

VALID_CATEGORIES = {CATEGORY_CASINO, CATEGORY_CRYPTO, CATEGORY_TECH, CATEGORY_UNRELATED}

# ── Gemini system prompt ──────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a B2B lead qualifier for BlocksRace, a company that provides
innovative betting markets and live sports content to the iGaming industry.
BlocksRace sells its betting content/markets to casino operators, sportsbooks,
and iGaming platforms. It may also partner with tech companies or attract
crypto/VC investment.

Given a company name, the contact's role, and a short web search snippet about
the company, classify the company into EXACTLY ONE of these four categories:

1. Casino Operator       — operates an online casino, sportsbook, or gambling platform
                           that could license BlocksRace betting markets
2. Crypto / VC          — a crypto firm, venture capital fund, or investor that might
                           fund or partner with BlocksRace
3. Strategic Tech Partner — a B2B tech provider (data, platform, software, API) that
                           could integrate with or complement BlocksRace
4. Unrelated            — affiliate, payment processor, slots/lottery provider, marketing
                           agency, regulator, recruitment firm, media, or simply too small
                           to be a meaningful partner

Also assign a Match Score from 1 to 5 reflecting how valuable this company is to
BlocksRace:
  5 = Casino Operator or Sportsbook actively needing new betting content
  4 = Platform / B2B iGaming tech with a clear integration angle
  3 = Crypto / VC with iGaming exposure or interest
  2 = Generic tech partner with some possible angle
  1 = Unrelated, affiliate, payment processor, or no clear path to revenue

Reply with ONLY a valid JSON object and nothing else — no markdown, no commentary:
{
  "category": "<one of the four categories above>",
  "score": <integer 1-5>,
  "rationale": "<one concise sentence explaining the score>"
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
    """Return rows where AI Evaluation text column is blank."""
    schema = notion_sync.get_schema()
    eval_type = schema.get(PROP_AI_EVAL)
    if eval_type == "rich_text":
        return {"property": PROP_AI_EVAL, "rich_text": {"is_empty": True}}
    if eval_type == "title":
        return {"property": PROP_AI_EVAL, "title": {"is_empty": True}}
    # Column missing — surface a clear error before touching any data.
    raise RuntimeError(
        f"Property '{PROP_AI_EVAL}' not found or wrong type (got {eval_type!r}). "
        "Add a Text column named 'AI Evaluation' to your Notion database."
    )


def fetch_unevaluated() -> list[dict]:
    """Page through the DB and return all rows where AI Evaluation is blank."""
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
                "page_id": page["id"],
                "name":    _plain_text(props.get(config.PROP_NAME)),
                "company": _plain_text(props.get(config.PROP_COMPANY)),
                "role":    _plain_text(props.get(config.PROP_ROLE)),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return contacts


def _write_result(page_id: str, category: str, score: int, rationale: str, status: str) -> None:
    """Patch all four AI text columns onto a Notion row."""
    schema = notion_sync.get_schema()

    def _rt(text: str) -> dict:
        return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}

    def _title_rt(text: str) -> dict:
        return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}

    properties: dict = {}

    for prop, value in [
        (PROP_AI_CATEGORY,  category),
        (PROP_AI_SCORE,     f"{score}/5"),
        (PROP_AI_RATIONALE, rationale),
        (PROP_AI_EVAL,      status),
    ]:
        ptype = schema.get(prop)
        if ptype == "rich_text":
            properties[prop] = _rt(value)
        elif ptype == "title":
            properties[prop] = _title_rt(value)
        # If the column is missing entirely, we silently skip it so one missing
        # column doesn't abort writes to the three that do exist.

    if not properties:
        print(f"  [eval ] WARNING: no writable AI columns found for page {page_id}.")
        return

    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    resp = notion_sync._notion_request("PATCH", url, {"properties": properties})
    resp.raise_for_status()


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

def _ddg_snippet(company: str) -> str:
    """Return a short text snippet about the company from DuckDuckGo.

    Tries two queries: an iGaming-flavoured search first (to surface industry
    signals quickly), then a plain name-only fallback. Returns the concatenated
    titles + bodies of the top 3 results (≤800 chars), or an empty string on
    failure. Failures are non-fatal — Gemini still runs with less context.
    """
    queries = [
        f'"{company}" casino OR sportsbook OR betting OR iGaming',
        f'"{company}"',
    ]
    for query in queries:
        try:
            with DDGS(timeout=20) as ddgs:
                results = ddgs.text(query, max_results=5) or []
            if results:
                parts = []
                for r in results[:3]:
                    title = r.get("title", "")
                    body  = r.get("body",  "")
                    if title:
                        parts.append(title)
                    if body:
                        parts.append(body)
                snippet = " | ".join(parts)
                return snippet[:800]
        except (RatelimitException, TimeoutException, DDGSException, Exception):
            pass
        time.sleep(2)
    return ""


# ── Gemini AI assessment ──────────────────────────────────────────────────────

def _gemini_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    return genai.Client(api_key=api_key)


def assess_company(client: genai.Client, company: str, role: str, snippet: str) -> dict:
    """Send company info to Gemini and parse the JSON response.

    Returns a dict with keys: category, score, rationale.
    Raises ValueError if Gemini returns unparseable output.
    """
    user_message = (
        f"Company: {company or '(unknown)'}\n"
        f"Contact role: {role or '(unknown)'}\n"
        f"Web context: {snippet or '(no search results found)'}"
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_message,
        config=genai_types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.1,
            max_output_tokens=256,
        ),
    )
    raw = (response.text or "").strip()

    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned non-JSON: {raw[:300]}") from exc

    category  = str(result.get("category", "")).strip()
    score_raw = result.get("score", 1)
    rationale = str(result.get("rationale", "")).strip()

    if category not in VALID_CATEGORIES:
        for valid in VALID_CATEGORIES:
            if valid.lower().split()[0] in category.lower():
                category = valid
                break
        else:
            category = CATEGORY_UNRELATED

    try:
        score = max(1, min(5, int(score_raw)))
    except (TypeError, ValueError):
        score = 1

    if not rationale:
        rationale = "No rationale provided."

    return {"category": category, "score": score, "rationale": rationale}


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
    for col in [PROP_AI_EVAL, PROP_AI_CATEGORY, PROP_AI_SCORE, PROP_AI_RATIONALE]:
        if schema.get(col) not in ("rich_text", "title", "text"):
            missing_cols.append(
                f"  '{col}' (type is {schema.get(col)!r} — needs to be a Text column)"
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

    done = stamped_from_cache = errors = 0
    company_num = 0

    for key, group in groups.items():
        company_num += 1
        # Use the first contact's company name as display label (original case)
        company = group[0]["company"] or group[0]["name"] or "(unknown)"
        role    = group[0]["role"]  # role of the first contact (for context)

        n_contacts = len(group)
        label = f"[company {company_num}/{len(groups)}] {company}"
        if n_contacts > 1:
            label += f"  ({n_contacts} contacts)"

        print(label)

        # ── DuckDuckGo snippet ──────────────────────────────────────────────
        snippet = _ddg_snippet(company)
        print(f"  Snippet : {snippet[:80]}{'…' if len(snippet) > 80 else ''}")

        # ── Gemini assessment ───────────────────────────────────────────────
        try:
            result = assess_company(client, company, role, snippet)
        except Exception as exc:
            print(f"  [ERROR] Gemini failed: {exc}")
            errors += len(group)
            # Stamp Skipped on all contacts in this group so we don't retry
            # forever; a human can clear the stamp to force a re-try.
            for contact in group:
                try:
                    _write_result(
                        contact["page_id"],
                        CATEGORY_UNRELATED, 1,
                        "Gemini error — review manually.",
                        "Skipped",
                    )
                except Exception:
                    pass
            _pace(company_num, len(groups))
            continue

        cat   = result["category"]
        score = result["score"]
        rat   = result["rationale"]
        print(f"  → {cat}  |  Score {score}/5  |  {rat}")

        # ── Write to all contacts in this group ─────────────────────────────
        first = True
        for contact in group:
            try:
                _write_result(contact["page_id"], cat, score, rat, "Done")
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


if __name__ == "__main__":
    run()
