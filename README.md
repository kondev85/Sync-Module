# Conference Scout

A Python toolkit that scrapes conference attendees from **Swapcard** into a **Notion** database, enriches LinkedIn profiles automatically, qualifies leads with **Gemini AI**, and provides a 24/7 **Telegram bot** for on-the-floor attendee lookup at live events.

Built for the iGB Live conference. The Telegram bot runs continuously on a Linux VPS via Docker, surviving crashes and server reboots automatically.

---

## Table of Contents

1. [What It Does — High-Level Overview](#1-what-it-does)
2. [Required API Keys & Secrets](#2-required-api-keys--secrets)
3. [Notion Database Setup](#3-notion-database-setup)
4. [VPS Installation (Docker)](#4-vps-installation-docker)
5. [Running Without Docker](#5-running-without-docker)
6. [Environment Variables Reference](#6-environment-variables-reference)
7. [Troubleshooting](#10-troubleshooting)

---

## 1. What It Does

The project has four distinct tools that can be run independently or together:

### Tool 1 — Swapcard → Notion Scraper (`RUN_MODE=scraper`)
Paginates through all attendees of an iGB Live event via Swapcard's private GraphQL API, enriches each person's profile with their job title, LinkedIn URL, and biography, then inserts or updates rows in your Notion database. Deduplication is by full name — re-running never creates duplicates, it updates existing rows.

### Tool 2 — LinkedIn Enricher (`RUN_MODE=enricher`)
Reads every Notion row where the LinkedIn column is empty, searches for the person's LinkedIn `/in/` profile using DuckDuckGo, Serper.dev, or Gemini AI (configurable), applies strict name + company verification guards to avoid writing wrong profiles, and writes the verified URL back to Notion.

### Tool 3 — AI Company Evaluator (`RUN_MODE=evaluator`)
For every unevaluated Notion row, fetches a short web snippet about the company (via DuckDuckGo), sends it to Gemini AI with a strict B2B lead-qualification prompt, and writes back a company type description, a 1–5 relevance score, a one-sentence rationale, and the company's official website domain. Companies are evaluated once and reused for every contact at the same company.

### Tool 4 — Conference Scout Telegram Bot (`RUN_MODE=bot`)
A 24/7 Telegram bot you use on-site at the conference. Send it a name or company name as a text message, or send a **photo of a conference badge**, and it returns:
- The person's Notion profile (company, role, LinkedIn, AI score)
- A live intelligence brief (recent news, funding, partnerships) via Gemini Google Search grounding
- A link to their profile photo
- A warning if they are already in your LinkedIn connections

---

## 2. Required API Keys & Secrets

All secrets are stored in a `.env` file in the project root on your VPS (or in Replit Secrets). **Never commit this file to git.**

### Always Required

| Secret | Where to Get It | Used By |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Message `@BotFather` on Telegram, create a new bot with `/newbot` | Telegram bot |
| `NOTION_API_TOKEN` | [notion.so/my-integrations](https://www.notion.so/my-integrations) → New Integration → copy the Internal Integration Secret | All tools |
| `NOTION_DATABASE_ID` | Open your Notion database in a browser → the ID is the 32-character string in the URL before the `?` | All tools |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) → Get API key | Bot, Evaluator, Enricher (Gemini backend) |

### Required Only for the Scraper

| Secret | Where to Get It | Notes |
|---|---|---|
| `SWAPCARD_BEARER_TOKEN` | Open the Swapcard event app in your browser → DevTools → Network tab → any GraphQL request → copy the `Authorization: Bearer ...` header value | Expires; refresh from browser if scraper 401s |
| `SWAPCARD_COOKIE` | Same DevTools Network request → copy the `Cookie` header value | Optional but improves reliability |

### Optional (LinkedIn Enricher alternative backends)

| Secret | Where to Get It | Used By |
|---|---|---|
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) → Register → API Key | LinkedIn enricher when `SEARCH_BACKEND=serper` |

> **Note:** DuckDuckGo (the default search backend) requires no API key. `SERPER_API_KEY` is only needed if you switch to the Serper backend for more reliable results. The Gemini backend uses your existing `GEMINI_API_KEY`.

---

## 3. Notion Database Setup

Your Notion database must have the following columns. Column names are **case-sensitive and must match exactly**.

| Column Name | Type | Required | Notes |
|---|---|---|---|
| `Name` | **Title** | Yes | Used for deduplication — must be the Title property |
| `Company` | Text | Recommended | Company name |
| `Role` | Text | Recommended | Job title |
| `LinkedIn` | URL | Recommended | LinkedIn profile URL |
| `Notes` | Text | Optional | Biography / notes |
| `iGBLive` | Checkbox | Optional | Checked for every synced attendee |
| `IGB URL` | URL | Optional | Direct link to the attendee's Swapcard profile page |
| `LinkedIn Enreacher` | Multi-select | Optional | Enricher stamp: `Yes`, `Skipped` (note: intentional spelling to match existing column) |
| `AI Evaluation` | Text | Required for evaluator | Gate column: blank = pending, `Done`, `Error`, `Skipped` |
| `AI Category` | Text | Required for evaluator | Gemini's 5–8 word company description |
| `AI Score` | Text | Required for evaluator | Gemini's score stored as `"4/5"` |
| `AI Rationale` | Text | Required for evaluator | Gemini's one-sentence explanation |
| `AI Web Context` | Text | Optional | Raw DuckDuckGo snippet saved before Gemini call |
| `Website Domain` | URL | Optional | Official company website found by Gemini |
| `Website Evaluator` | Text | Optional | Website pass stamp: blank, `Done`, `Skipped` |

**Integration permission:** Go to your Notion database → `...` menu → Add connections → select your integration. Without this step the API returns 404.

---

## 4. VPS Installation (Docker)

These instructions are for a Linux VPS (tested on Hetzner Ubuntu/Debian). Docker and Docker Compose must be installed.

### Step 1 — Clone the repository

```bash
git clone <your-repo-url>
cd <repo-folder>
```

### Step 2 — Create the `.env` file

```bash
cp .env.example .env   # or create it from scratch
nano .env
```

Paste your secrets into `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
NOTION_API_TOKEN=secret_...
NOTION_DATABASE_ID=abcdef1234567890abcdef1234567890
GEMINI_API_KEY=AIza...

# Only needed for the scraper:
SWAPCARD_BEARER_TOKEN=eyJ...
SWAPCARD_COOKIE=session=...; cf_clearance=...
```

### Step 3 — Create required data files

These files must exist in the repo root **before** starting Docker, otherwise Docker creates them as empty directories:

```bash
touch my_profile.md
touch Connections.csv
```

- **`my_profile.md`** — Paste your professional background in Markdown. The bot uses this to generate personalised opening lines. You can fill it in later; the bot works without it.
- **`Connections.csv`** — Export your LinkedIn connections: LinkedIn → My Network → Connections → Manage → Export. The bot uses this to alert you when you already know someone. Optional.

### Step 4 — Build and start the bot

```bash
docker compose up -d --build
```

This builds the Docker image and starts the `bot` service in the background. The bot runs continuously and restarts automatically after crashes or reboots.

### Step 5 — Verify it is running

```bash
docker compose ps          # should show "running"
docker compose logs -f bot  # live log stream; press Ctrl+C to stop
```


---

## 5. Running Without Docker

From a shell in the `swapcard_sync/` directory:

```bash
cd swapcard_sync
pip install -r requirements.txt
python -u main.py
```

This shows an interactive menu:

```
============================================================
  iGB Live — Contact Tools
============================================================
  [1] Run iGB Live Event Scraper & Sync
  [2] Run Missing LinkedIn Profiles Finder & Enricher
  [3] Run AI Company Evaluator (BlocksRace lead qualifier)
  [4] Run Website Domain Finder (already-evaluated companies)
  [5] Run Conference Scout Telegram Bot
------------------------------------------------------------
```

For non-interactive / scripted runs, set `RUN_MODE` to skip the menu:

```bash
RUN_MODE=scraper  python -u main.py   # runs the scraper
RUN_MODE=enricher python -u main.py   # runs the LinkedIn enricher
RUN_MODE=evaluator python -u main.py  # runs the AI evaluator
RUN_MODE=website   python -u main.py  # runs the website domain finder
RUN_MODE=bot       python -u main.py  # starts the Telegram bot
```

## 6. Environment Variables Reference

All variables are optional unless marked **required**. Set them in `.env` or pass them inline.

### Credentials

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot only | — | Telegram bot token from @BotFather |
| `NOTION_API_TOKEN` | **Yes** | — | Notion integration secret |
| `NOTION_DATABASE_ID` | **Yes** | — | ID of the target Notion database |
| `GEMINI_API_KEY` | Bot + Evaluator | — | Google AI Studio API key |
| `SWAPCARD_BEARER_TOKEN` | Scraper only | — | Bearer token captured from browser DevTools |
| `SWAPCARD_COOKIE` | Scraper only | — | Cookie header captured from browser DevTools |
| `SERPER_API_KEY` | Optional | — | Serper.dev API key (only needed if `SEARCH_BACKEND=serper`) |


---

## 7. Troubleshooting

### `PersistedQueryNotFound` from Swapcard
The API hash has rotated. Swapcard deploys a new web client and the hash changes. Open the Swapcard event URL in your browser, open DevTools → Network, filter for `graphql`, make any action (scroll the attendee list), and copy the new `sha256Hash` and `operationName` values. Set them via `SWAPCARD_PERSISTED_QUERY_HASH` and `SWAPCARD_OPERATION_NAME` (and the detail equivalents).

### Scraper returns 401 Unauthorized
Your `SWAPCARD_BEARER_TOKEN` has expired. Open the Swapcard event in your browser, go to DevTools → Network → any GraphQL request → copy the `Authorization` header value again.

### LinkedIn enricher writes wrong profiles
Enable debug mode: `DEBUG_ENRICHER=1`. Every query, result URL, title, and the exact rejection reason (no-linkedin-url / name-mismatch / no-company-corroboration) will be printed. The most common cause is a company field with two companies packed together (e.g. `"A Ltd / B.com"`) — the enricher handles this with multi-company splitting, but very short or generic company names may not have identifying tokens to corroborate.

### Gemini 429 rate limit errors
The evaluator and enricher both auto-retry with the exact delay Gemini requests in the error body. If you see repeated 429s, raise `EVAL_INTERVAL` (evaluator) or `GEMINI_SEARCH_INTERVAL` (enricher Gemini backend), or reduce `MAX_EVALUATIONS`/`MAX_LOOKUPS` to run smaller batches.

### Bot not responding
1. `docker compose ps` — check it is running.
2. `docker compose logs -f bot` — look for errors.
3. If you see `Conflict` in the logs, another bot instance is running with the same token. Kill all other instances and restart.
4. Verify all four required secrets are in `.env`.

### Notion writes failing with `object_not_found`
Your Notion integration does not have access to the database. Go to the database in Notion → `...` menu → Add connections → select your integration name.

### Long scraper runs killed after ~2 minutes
You are running the scraper from the Replit Agent sandbox, which kills long-running processes. Use the **Shell tab** instead, or run via Docker (`docker compose run --rm scraper`).

### Resuming an interrupted scraper run
Look at the last index number printed in the logs (e.g. `[1847]`). Subtract ~20 for safety, then run:
```bash
SKIP_CONTACTS=1827 ENRICH_PROFILES=1 python -u main.py
```
A small overlap is safe — the scraper deduplicates by name and updates existing rows rather than creating duplicates.
