"""Qualify IGB Live leads using DuckDuckGo context + Gemini AI.

For each Notion row where 'AI Evaluation' is blank:
  1. Grab Company Name + Role from the row.
  2. Search DuckDuckGo for a short business-context snippet.
  3. Send that context to Gemini Flash (free tier) with a strict system prompt.
  4. Gemini returns: category, score (1-5), rationale (1 sentence).
  5. Write those three values back to Notion + stamp 'AI Evaluation = Done'.

Run from the Shell (not the Agent sandbox — long runs need a real terminal):
  cd swapcard_sync && python -u company_evaluator.py

Env toggles:
  MAX_EVALUATIONS  — cap how many rows to process this run (0 = unlimited)
  EVAL_INTERVAL    — seconds between rows (default 3.0)
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

# ── Column names (must match Notion DB exactly) ─────────────────────────────
PROP_AI_EVAL     = "AI Evaluation"   # select — gate: blank → process, Done/Skipped → skip
PROP_AI_CATEGORY = "AI Category"     # select
PROP_AI_SCORE    = "AI Score"        # number
PROP_AI_RATIONALE = "AI Rationale"  # rich_text

# ── Run settings ─────────────────────────────────────────────────────────────
MAX_EVALUATIONS = max(0, int(os.environ.get("MAX_EVALUATIONS", "0")))  # 0 = unlimited
EVAL_INTERVAL   = max(0.0, float(os.environ.get("EVAL_INTERVAL", "3.0")))
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# ── Category labels (must match the Notion select options you create) ─────────
CATEGORY_CASINO   = "Casino Operator"
CATEGORY_CRYPTO   = "Crypto / VC"
CATEGORY_TECH     = "Strategic Tech Partner"
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


def _build_filter() -> dict:
    """Return rows where AI Evaluation is blank (never processed)."""
    schema = notion_sync.get_schema()
    eval_type = schema.get(PROP_AI_EVAL)
    if eval_type in ("select", "status"):
        return {
            "property": PROP_AI_EVAL,
            eval_type: {"is_empty": True},
        }
    # Column missing or wrong type — surface a clear error rather than
    # silently processing everything.
    raise RuntimeError(
        f"Property '{PROP_AI_EVAL}' not found or wrong type (got {eval_type!r}). "
        "Create a 'Select' property named 'AI Evaluation' in your Notion database "
        "with options: Done, Skipped."
    )


def fetch_unevaluated() -> list[dict]:
    """Page through the DB and return rows where AI Evaluation is blank."""
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
    """Patch AI Category, AI Score, AI Rationale, and AI Evaluation onto a row."""
    schema = notion_sync.get_schema()
    properties: dict = {}

    if schema.get(PROP_AI_CATEGORY) == "select":
        properties[PROP_AI_CATEGORY] = {"select": {"name": category}}

    if schema.get(PROP_AI_SCORE) == "number":
        properties[PROP_AI_SCORE] = {"number": score}

    if schema.get(PROP_AI_RATIONALE) in ("rich_text", "text"):
        properties[PROP_AI_RATIONALE] = {
            "rich_text": [{"type": "text", "text": {"content": rationale[:2000]}}]
        }

    eval_type = schema.get(PROP_AI_EVAL)
    if eval_type == "select":
        properties[PROP_AI_EVAL] = {"select": {"name": status}}
    elif eval_type == "status":
        properties[PROP_AI_EVAL] = {"status": {"name": status}}

    if not properties:
        print("  [eval ] WARNING: no writable AI columns found — check your Notion schema.")
        return

    url = f"{config.NOTION_API_URL}/pages/{page_id}"
    resp = notion_sync._notion_request("PATCH", url, {"properties": properties})
    resp.raise_for_status()


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

def _ddg_snippet(company: str) -> str:
    """Return a short text snippet about the company from DuckDuckGo.

    Tries two queries: one iGaming-flavoured (to surface industry signals
    quickly), then a plain name-only fallback. Returns the concatenated
    titles + bodies of the top 3 results (≤800 chars), or empty string on
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
        # Best-effort fuzzy match before giving up
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


# ── Main run loop ─────────────────────────────────────────────────────────────

def run() -> None:
    validate()
    notion_sync.get_schema()  # warm the cache + validate DB is reachable

    print("\n=== Company Evaluator (BlocksRace lead qualifier) ===")
    print(f"Model : {GEMINI_MODEL}")
    print(f"Pacing: {EVAL_INTERVAL}s between rows")
    if MAX_EVALUATIONS:
        print(f"Cap   : {MAX_EVALUATIONS} rows this run")
    print()

    # ── Check schema up front ──────────────────────────────────────────────
    schema = notion_sync.get_schema()
    missing_cols = []
    for col, expected in [
        (PROP_AI_EVAL,      ("select", "status")),
        (PROP_AI_CATEGORY,  ("select",)),
        (PROP_AI_SCORE,     ("number",)),
        (PROP_AI_RATIONALE, ("rich_text", "text")),
    ]:
        actual = schema.get(col)
        if actual not in expected:
            missing_cols.append(f"  '{col}' (need {expected}, got {actual!r})")
    if missing_cols:
        print("WARNING — the following Notion columns are missing or wrong type:")
        for m in missing_cols:
            print(m)
        print(
            "\nPlease add them in Notion before running a full batch. "
            "See the instructions at the top of this file.\n"
        )

    print("Fetching unevaluated rows from Notion…")
    contacts = fetch_unevaluated()
    total = len(contacts)
    print(f"Found {total} rows to evaluate.\n")

    if not contacts:
        print("Nothing to do — all rows already have an AI Evaluation stamp.")
        return

    if MAX_EVALUATIONS:
        contacts = contacts[:MAX_EVALUATIONS]
        print(f"Capped to first {len(contacts)} rows for this run.\n")

    client = _gemini_client()

    done = skipped = errors = 0
    for i, contact in enumerate(contacts, 1):
        company = contact["company"] or contact["name"] or "(unknown)"
        role    = contact["role"]
        page_id = contact["page_id"]
        label   = f"[{i}/{len(contacts)}] {company}"

        print(f"{label}")
        print(f"  Role   : {role or '—'}")

        # 1. DuckDuckGo snippet
        snippet = _ddg_snippet(company)
        print(f"  Snippet: {snippet[:80]}{'…' if len(snippet) > 80 else ''}")

        # 2. Gemini assessment
        try:
            result = assess_company(client, company, role, snippet)
        except Exception as exc:
            print(f"  [ERROR] Gemini failed: {exc}")
            errors += 1
            # Stamp Skipped so we don't retry forever on a broken row,
            # but don't fill category/score so a human can review.
            try:
                _write_result(page_id, CATEGORY_UNRELATED, 1, "Gemini error — review manually.", "Skipped")
            except Exception:
                pass
            _pace(i, len(contacts))
            continue

        cat   = result["category"]
        score = result["score"]
        rat   = result["rationale"]
        print(f"  → {cat}  |  Score {score}/5  |  {rat}")

        # 3. Write back to Notion
        try:
            _write_result(page_id, cat, score, rat, "Done")
            done += 1
        except requests.RequestException as exc:
            print(f"  [ERROR] Notion write failed: {exc}")
            errors += 1

        _pace(i, len(contacts))

    print(f"\n✓ Finished.  Done: {done}  Skipped: {skipped}  Errors: {errors}")


def _pace(index: int, total: int) -> None:
    """Sleep between rows; skip the pause after the last one."""
    if index < total and EVAL_INTERVAL > 0:
        jitter = random.uniform(-0.3, 0.3) * EVAL_INTERVAL
        time.sleep(max(0.0, EVAL_INTERVAL + jitter))


if __name__ == "__main__":
    run()
