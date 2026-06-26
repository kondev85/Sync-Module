"""
telegram_bot.py — Conference Scout Bot for BlocksRace

Queries the Notion CRM for attendee / company data, enriches with Gemini AI
evaluation, checks your LinkedIn network, and scans conference badges via
multimodal Gemini.

Run standalone:  python -u telegram_bot.py
Via main.py:     RUN_MODE=bot python -u main.py

Required env vars
-----------------
TELEGRAM_BOT_TOKEN   — from @BotFather
NOTION_API_TOKEN     — Notion integration token
NOTION_DATABASE_ID   — target Contacts database
GEMINI_API_KEY       — Google AI Studio key

Optional env vars
-----------------
GEMINI_MODEL         — scoring / opening-line model (default: gemini-2.5-flash)
GEMINI_SCOUT_MODEL   — OCR model for badge scanning (default: gemini-2.0-flash-lite)

Local data files (place in swapcard_sync/ alongside this file)
--------------------------------------------------------------
my_profile.md        — your background; enables personalised opening lines
Connections.csv      — LinkedIn "Connections" CSV export; enables network alerts
"""

import asyncio
import csv
import html
import io
import json
import logging
import os
import pathlib
import re

from google import genai
from google.genai import types as genai_types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import config
import notion_sync

# ── Constants ──────────────────────────────────────────────────────────────────

_BOT_DIR = pathlib.Path(__file__).parent

TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL        = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_SCOUT_MODEL  = os.environ.get("GEMINI_SCOUT_MODEL", "gemini-2.0-flash-lite")

MY_PROFILE_PATH     = _BOT_DIR / "my_profile.md"
CONNECTIONS_PATH    = _BOT_DIR / "Connections.csv"

PROP_AI_EVAL      = "AI Evaluation"
PROP_AI_CATEGORY  = "AI Category"
PROP_AI_SCORE     = "AI Score"
PROP_AI_RATIONALE = "AI Rationale"

logger = logging.getLogger(__name__)


# ── Startup: load static assets ───────────────────────────────────────────────

def _load_my_profile() -> str:
    if MY_PROFILE_PATH.exists():
        return MY_PROFILE_PATH.read_text(encoding="utf-8").strip()
    return ""


def _load_connections() -> set[str]:
    """Return lowercase full-name set from a LinkedIn Connections CSV export."""
    names: set[str] = set()
    if not CONNECTIONS_PATH.exists():
        return names
    with open(CONNECTIONS_PATH, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            first = (row.get("First Name") or "").strip()
            last  = (row.get("Last Name")  or "").strip()
            full  = f"{first} {last}".strip()
            if full:
                names.add(full.lower())
    return names


MY_PROFILE:  str       = _load_my_profile()
CONNECTIONS: set[str]  = _load_connections()


# ── Gemini ─────────────────────────────────────────────────────────────────────

_EVAL_SYSTEM_PROMPT = """You are a B2B lead qualifier for BlocksRace, a company that provides
innovative betting markets and live sports content to the iGaming industry.
BlocksRace sells its betting content/markets to casino operators, sportsbooks,
and iGaming platforms. It may also partner with tech companies or attract
crypto/VC investment.

Given a company name, the contact's role, and any web context you can find,
produce exactly three things:

1. company_type — a precise 5-8 word description of exactly what this company does.
   Plain noun phrase, lowercase, no punctuation.
   Examples: "online casino and sportsbook operator", "sports betting data API provider"

2. score — an integer 1-5:
   5 = Casino Operator or Sportsbook actively needing new betting content
   4 = B2B iGaming tech platform with a clear integration angle
   3 = Crypto / VC with iGaming exposure or interest
   2 = Adjacent — could be a useful partner in the future
   1 = No integration path (payment processors, PSPs, agencies, media, regulators)
   IMPORTANT: Payment processors and PSPs are ALWAYS score 1.

3. rationale — one concise sentence explaining the score.

Reply with ONLY valid JSON, no markdown fences or commentary:
{"company_type": "...", "score": <1-5>, "rationale": "..."}"""


def _gemini_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    return genai.Client(api_key=GEMINI_API_KEY)


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n?", "", text.strip())
    return re.sub(r"\n?```$", "", text).strip()


def gemini_score_grounded(client: genai.Client, name: str, company: str, role: str) -> dict:
    """
    Real-time lead evaluation using Gemini with Google Search grounding.
    Returns dict with keys: company_type, score, rationale.
    """
    prompt = (
        f"Contact: {name or 'Unknown'}\n"
        f"Company: {company or 'Unknown'}\n"
        f"Role: {role or 'Unknown'}\n\n"
        "Search the web for this company, then produce the JSON evaluation."
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_EVAL_SYSTEM_PROMPT,
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
            ),
        )
        result = json.loads(_strip_fences(response.text))
        score = max(1, min(5, int(result.get("score", 1))))
        return {
            "company_type": str(result.get("company_type", "")).strip(),
            "score": score,
            "rationale": str(result.get("rationale", "")).strip(),
        }
    except Exception as exc:
        logger.warning("gemini_score_grounded error: %s", exc)
        return {"company_type": "unknown", "score": 0, "rationale": f"Gemini error: {exc}"}


def gemini_opening_lines(client: genai.Client, contact: dict) -> list[str]:
    """
    Generate 2 personalised opening lines using my_profile.md context.
    Returns a list of up to 2 strings; empty list if profile not loaded.
    """
    if not MY_PROFILE:
        return []
    prompt = (
        f"My background (I will start this conversation):\n{MY_PROFILE}\n\n"
        f"Person I am about to meet at a conference:\n"
        f"  Name:         {contact.get('name', '')}\n"
        f"  Company:      {contact.get('company', '')}\n"
        f"  Role:         {contact.get('role', '')}\n"
        f"  Company type: {contact.get('ai_category', '')}\n\n"
        "Write exactly 2 short, natural opening lines I can say on the conference floor. "
        "Look for genuine mutual touchpoints (geography, background, industry overlap). "
        "Each line must be 1–2 sentences. "
        "Return ONLY a JSON array of 2 strings: [\"line1\", \"line2\"]"
    )
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        lines = json.loads(_strip_fences(response.text))
        if isinstance(lines, list) and len(lines) >= 2:
            return [str(lines[0]).strip(), str(lines[1]).strip()]
    except Exception as exc:
        logger.warning("gemini_opening_lines error: %s", exc)
    return []


def gemini_ocr_badge(client: genai.Client, image_bytes: bytes) -> dict:
    """
    Pass image bytes to Gemini multimodal to extract conference badge text.
    Returns dict: {name, company, role, raw}.
    """
    prompt = (
        "This is a photo of a conference name badge. "
        "Extract all visible text. "
        "Return ONLY a JSON object with keys: "
        "\"name\" (full name on the badge), "
        "\"company\" (company or organisation), "
        "\"role\" (job title or role), "
        "\"raw\" (full verbatim text from the badge). "
        "Use empty string for any field not visible."
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_SCOUT_MODEL,
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt,
            ],
        )
        return json.loads(_strip_fences(response.text))
    except Exception as exc:
        logger.warning("gemini_ocr_badge error: %s", exc)
        return {"name": "", "company": "", "role": "", "raw": str(exc)}


# ── Notion search helpers ─────────────────────────────────────────────────────

def _plain_text(prop: dict | None) -> str:
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _url_val(prop: dict | None) -> str:
    if not prop:
        return ""
    return (prop.get("url") or "").strip()


def _select_val(prop: dict | None) -> str:
    if not prop:
        return ""
    opts = prop.get("multi_select") or []
    if opts:
        return ", ".join(o.get("name", "") for o in opts)
    sel = prop.get("select") or {}
    return sel.get("name", "")


def _extract_page(page: dict) -> dict:
    p = page.get("properties", {})
    return {
        "page_id":          page["id"],
        "name":             _plain_text(p.get(config.PROP_NAME)),
        "company":          _plain_text(p.get(config.PROP_COMPANY)),
        "role":             _plain_text(p.get(config.PROP_ROLE)),
        "linkedin":         _url_val(p.get(config.PROP_LINKEDIN)),
        "linkedin_status":  _select_val(p.get(config.PROP_LINKEDIN_STATUS)),
        "ai_category":      _plain_text(p.get(PROP_AI_CATEGORY)),
        "ai_score":         _plain_text(p.get(PROP_AI_SCORE)),
        "ai_rationale":     _plain_text(p.get(PROP_AI_RATIONALE)),
        "ai_eval":          _plain_text(p.get(PROP_AI_EVAL)),
    }


def _notion_query(body: dict) -> list[dict]:
    url = f"{config.NOTION_API_URL}/databases/{config.NOTION_DATABASE_ID}/query"
    resp = notion_sync._notion_request("POST", url, body)
    resp.raise_for_status()
    return [_extract_page(p) for p in resp.json().get("results", [])]


def search_by_name_exact(name: str) -> list[dict]:
    return _notion_query({
        "filter": {"property": config.PROP_NAME, "title": {"equals": name}},
        "page_size": 5,
    })


def search_by_name_fuzzy(name: str) -> list[dict]:
    return _notion_query({
        "filter": {"property": config.PROP_NAME, "title": {"contains": name}},
        "page_size": 10,
    })


def search_by_company(company: str) -> list[dict]:
    return _notion_query({
        "filter": {"property": config.PROP_COMPANY, "rich_text": {"contains": company}},
        "page_size": 25,
    })


# ── LinkedIn connection check ─────────────────────────────────────────────────

def is_connected(name: str) -> bool:
    return bool(name) and name.strip().lower() in CONNECTIONS


# ── Message formatting (HTML) ─────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s or ""))


_SCORE_EMOJI = {5: "🔥", 4: "🟢", 3: "🟡", 2: "🟠", 1: "🔴"}


def _score_emoji(score_raw) -> str:
    try:
        return _SCORE_EMOJI.get(int(str(score_raw).split("/")[0].strip()), "⚪")
    except (ValueError, TypeError):
        return "⚪"


def _linkedin_line(contact: dict) -> str:
    li = contact.get("linkedin", "")
    if li:
        return f'🔗 <a href="{_e(li)}">LinkedIn Profile</a>'
    if contact.get("linkedin_status") == "Skipped":
        return "🔗 LinkedIn: not found (searched)"
    return "🔗 LinkedIn: not in database"


def format_single_profile(contact: dict, opening_lines: list[str], connected: bool) -> str:
    score_raw = contact.get("ai_score", "")
    emoji = _score_emoji(score_raw)
    lines = []

    if connected:
        lines.append("⚠️ <b>NETWORK HIT: Already connected on LinkedIn.</b>\n")

    lines.append(f"👤 <b>{_e(contact.get('name', '—'))}</b>")
    if contact.get("company"):
        lines.append(f"🏢 {_e(contact['company'])}")
    if contact.get("role"):
        lines.append(f"💼 {_e(contact['role'])}")
    lines.append("")

    has_eval = contact.get("ai_category") or score_raw
    if has_eval:
        if contact.get("ai_category"):
            lines.append(f"{emoji} <b>{_e(contact['ai_category'])}</b>")
        if score_raw:
            lines.append(f"📊 Score: <b>{_e(score_raw)}</b>")
        if contact.get("ai_rationale"):
            lines.append(f"💡 {_e(contact['ai_rationale'])}")
    else:
        lines.append("⚪ Not yet AI-evaluated")
    lines.append("")

    lines.append(_linkedin_line(contact))

    if opening_lines:
        lines.append("")
        lines.append("🗣️ <b>Opening lines:</b>")
        for i, line in enumerate(opening_lines, 1):
            lines.append(f"{i}. {_e(line)}")

    return "\n".join(lines)


def format_company_results(query: str, contacts: list[dict]) -> str:
    best = None
    for c in contacts:
        if c.get("ai_score") and c.get("ai_category"):
            if best is None:
                best = c
            else:
                try:
                    if int(str(c["ai_score"]).split("/")[0]) > int(str(best["ai_score"]).split("/")[0]):
                        best = c
                except ValueError:
                    pass

    lines = [f'🔍 Company: <b>{_e(query)}</b> — {len(contacts)} contact(s) found\n']

    if best and best.get("ai_category"):
        score_raw = best.get("ai_score", "")
        emoji = _score_emoji(score_raw)
        lines.append(f"{emoji} <b>{_e(best['ai_category'])}</b>")
        if score_raw:
            lines.append(f"📊 Score: <b>{_e(score_raw)}</b>")
        if best.get("ai_rationale"):
            lines.append(f"💡 {_e(best['ai_rationale'])}")
        lines.append("")

    lines.append("👥 <b>Contacts in database:</b>")
    for c in contacts:
        name        = c.get("name", "—")
        role        = c.get("role", "")
        li_flag     = " 🔗" if c.get("linkedin") else ""
        net_flag    = " ⚠️" if is_connected(name) else ""
        role_part   = f" — {_e(role)}" if role else ""
        lines.append(f"• {_e(name)}{role_part}{li_flag}{net_flag}")

    lines.append("")
    lines.append("<i>Reply with a full name for the complete profile.</i>")
    return "\n".join(lines)


def format_gemini_fallback(
    name: str, company: str, role: str,
    eval_result: dict, opening_lines: list[str], connected: bool,
) -> str:
    emoji = _score_emoji(eval_result.get("score", 0))
    lines = []

    if connected:
        lines.append("⚠️ <b>NETWORK HIT: Already connected on LinkedIn.</b>\n")

    lines.append("🌐 <b>Not in Notion — live Gemini web search:</b>\n")

    if name:
        lines.append(f"👤 <b>{_e(name)}</b>")
    if company:
        lines.append(f"🏢 {_e(company)}")
    if role:
        lines.append(f"💼 {_e(role)}")
    lines.append("")

    cat   = eval_result.get("company_type", "unknown")
    score = eval_result.get("score", 0)
    rat   = eval_result.get("rationale", "")

    lines.append(f"{emoji} <b>{_e(cat)}</b>")
    if score:
        lines.append(f"📊 Score: <b>{score}/5</b>")
    if rat:
        lines.append(f"💡 {_e(rat)}")

    if opening_lines:
        lines.append("")
        lines.append("🗣️ <b>Opening lines:</b>")
        for i, line in enumerate(opening_lines, 1):
            lines.append(f"{i}. {_e(line)}")

    return "\n".join(lines)


# ── Core search pipeline (synchronous — run in executor) ─────────────────────

def search_pipeline(query: str, client: genai.Client) -> str:
    """
    Search flow:
      1. Exact name match in Notion          → single profile card
      2. Company contains match in Notion    → Option A: AI card + name list (if >1)
      3. Fuzzy name contains in Notion       → single or list
      4. Nothing found                       → Gemini Search Grounding fallback
    At each step, checks LinkedIn connections and generates opening lines.
    """
    query = query.strip()

    # 1. Exact name match
    results = search_by_name_exact(query)
    if results:
        c = results[0]
        connected = is_connected(c.get("name", ""))
        opening   = gemini_opening_lines(client, c)
        return format_single_profile(c, opening, connected)

    # 2. Company search
    company_hits = search_by_company(query)
    if len(company_hits) > 1:
        return format_company_results(query, company_hits)
    if len(company_hits) == 1:
        c = company_hits[0]
        connected = is_connected(c.get("name", ""))
        opening   = gemini_opening_lines(client, c)
        return format_single_profile(c, opening, connected)

    # 3. Fuzzy name contains
    fuzzy = search_by_name_fuzzy(query)
    if len(fuzzy) > 1:
        return format_company_results(query, fuzzy)
    if len(fuzzy) == 1:
        c = fuzzy[0]
        connected = is_connected(c.get("name", ""))
        opening   = gemini_opening_lines(client, c)
        return format_single_profile(c, opening, connected)

    # 4. Gemini Search Grounding fallback
    eval_result = gemini_score_grounded(client, name=query, company=query, role="")
    connected   = is_connected(query)
    opening     = gemini_opening_lines(client, {
        "name": query, "company": query, "role": "",
        "ai_category": eval_result.get("company_type", ""),
    })
    return format_gemini_fallback(query, query, "", eval_result, opening, connected)


def badge_search_pipeline(name: str, company: str, role: str, client: genai.Client) -> str:
    """Same pipeline as search_pipeline but with pre-extracted name / company / role."""
    results = search_by_name_exact(name) if name else []
    if not results and company:
        results = search_by_company(company)
    if not results and name:
        results = search_by_name_fuzzy(name)

    if len(results) > 1:
        return format_company_results(company or name, results)
    if len(results) == 1:
        c = results[0]
        connected = is_connected(c.get("name", ""))
        opening   = gemini_opening_lines(client, c)
        return format_single_profile(c, opening, connected)

    eval_result = gemini_score_grounded(client, name, company, role)
    connected   = is_connected(name)
    opening     = gemini_opening_lines(client, {
        "name": name, "company": company, "role": role,
        "ai_category": eval_result.get("company_type", ""),
    })
    return format_gemini_fallback(name, company, role, eval_result, opening, connected)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.message.text or "").strip()
    if not query:
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    client = _gemini_client()
    loop   = asyncio.get_event_loop()

    try:
        reply = await loop.run_in_executor(None, search_pipeline, query, client)
    except Exception as exc:
        logger.exception("search_pipeline error")
        reply = f"❌ Error: {_e(str(exc))}"

    await update.message.reply_text(
        reply, parse_mode="HTML", disable_web_page_preview=True
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    client = _gemini_client()
    loop   = asyncio.get_event_loop()

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    image_bytes = buf.getvalue()

    await update.message.reply_text("📸 Scanning badge…")
    await update.message.chat.send_action(ChatAction.TYPING)

    badge = await loop.run_in_executor(None, gemini_ocr_badge, client, image_bytes)

    name    = badge.get("name", "").strip()
    company = badge.get("company", "").strip()
    role    = badge.get("role", "").strip()

    if not name and not company:
        raw = badge.get("raw", "(empty)")
        await update.message.reply_text(
            f"⚠️ Could not read badge text.\n\n<pre>{_e(raw)}</pre>",
            parse_mode="HTML",
        )
        return

    label = f"<b>{_e(name)}</b>" if name else f"<b>{_e(company)}</b>"
    if name and company:
        label = f"<b>{_e(name)}</b> @ <b>{_e(company)}</b>"

    await update.message.reply_text(
        f"🔍 Badge read: {label} — searching Notion…",
        parse_mode="HTML",
    )
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        reply = await loop.run_in_executor(
            None, badge_search_pipeline, name, company, role, client
        )
    except Exception as exc:
        logger.exception("badge_search_pipeline error")
        reply = f"❌ Error: {_e(str(exc))}"

    await update.message.reply_text(
        reply, parse_mode="HTML", disable_web_page_preview=True
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    """Start the bot. Called by main.py when RUN_MODE=bot, or directly."""
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "GEMINI_API_KEY":     GEMINI_API_KEY,
        "NOTION_API_TOKEN":   config.NOTION_API_TOKEN,
        "NOTION_DATABASE_ID": config.NOTION_DATABASE_ID,
    }.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        level=logging.INFO,
    )

    profile_status = (
        f"my_profile.md loaded ({len(MY_PROFILE)} chars)"
        if MY_PROFILE else "my_profile.md not found — opening lines disabled"
    )
    conn_status = (
        f"Connections.csv loaded ({len(CONNECTIONS)} names)"
        if CONNECTIONS else "Connections.csv not found — LinkedIn alerts disabled"
    )
    logger.info("Conference Scout starting…")
    logger.info(profile_status)
    logger.info(conn_status)
    logger.info("Model (scoring/opening lines): %s", GEMINI_MODEL)
    logger.info("Model (badge OCR):             %s", GEMINI_SCOUT_MODEL)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Polling for messages…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
