# BlocksRace Conference Scout

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
7. [Module-by-Module Function Reference](#7-module-by-module-function-reference)
   - [config.py](#configpy)
   - [main.py](#mainpy)
   - [notion_sync.py](#notion_syncpy)
   - [linkedin_enricher.py](#linkedin_enricherpy)
   - [company_evaluator.py](#company_evaluatorpy)
   - [telegram_bot.py](#telegram_botpy)
8. [Data Files](#8-data-files)
9. [Docker Services Summary](#9-docker-services-summary)
10. [Troubleshooting](#10-troubleshooting)

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

### Day-to-day operations

| Task | Command |
|---|---|
| View live logs | `docker compose logs -f bot` |
| Restart bot | `docker compose restart bot` |
| Stop bot | `docker compose stop bot` |
| Rebuild after a code change | `docker compose up -d --build bot` |
| Update `my_profile.md` or `Connections.csv` | Edit the files directly on the VPS — no rebuild needed, changes are live immediately |

### Running the one-shot jobs (Scraper, Enricher, Evaluator)

These are separate Docker services that do not start automatically. Run them manually:

```bash
# Swapcard → Notion scraper
docker compose run --rm scraper

# LinkedIn URL enricher
docker compose run --rm enricher

# AI company evaluator
docker compose run --rm evaluator
```

Override settings for a test run:

```bash
# Scrape only the first 50 attendees
docker compose run --rm -e MAX_CONTACTS=50 scraper

# Enrich only 10 profiles
docker compose run --rm -e MAX_LOOKUPS=10 enricher

# Resume scraper from attendee #200
docker compose run --rm -e SKIP_CONTACTS=200 scraper
```

> **Important:** Full scraper runs (5,000+ attendees) take 1–2 hours. Run them via Docker or a Shell tab — never from the Replit Agent sandbox, which kills processes after ~120 seconds.

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

Recommended scraper command with pacing:

```bash
ENRICH_PROFILES=1 ROW_INSERT_INTERVAL=0.6 python -u main.py
```

Resume after interruption (replace `200` with the last index shown in the logs minus ~20):

```bash
SKIP_CONTACTS=200 ENRICH_PROFILES=1 python -u main.py
```

---

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

### Swapcard API Parameters (rarely need changing)

| Variable | Default | Description |
|---|---|---|
| `SWAPCARD_VIEW_ID` | `RXZlbnRWaWV3XzEyNjYyNzU=` | Event view to scrape |
| `SWAPCARD_EVENT_ID` | `RXZlbnRfNDM5NTcyNw==` | Event ID for profile detail queries |
| `SWAPCARD_OPERATION_NAME` | `EventPeopleListViewConnectionQuery` | GraphQL operation name for people list |
| `SWAPCARD_PERSISTED_QUERY_HASH` | `c5db63...` | SHA-256 hash for people list persisted query |
| `SWAPCARD_DETAIL_OPERATION_NAME` | `EventPersonDetailsQuery` | GraphQL operation name for individual profiles |
| `SWAPCARD_DETAIL_PERSISTED_QUERY_HASH` | `7b56a3...` | SHA-256 hash for profile detail persisted query |
| `IGB_LIVE_PERSON_BASE_URL` | `https://event.igblive.com/event/igb-live-2026/person/` | Base URL for attendee profile links |

> If Swapcard redeploys their web client, the hashes rotate and requests return `PersistedQueryNotFound`. Re-capture the new hash from the browser's Network tab.

### Scraper Controls

| Variable | Default | Description |
|---|---|---|
| `ENRICH_PROFILES` | `1` | `1` = fetch full profile (role + LinkedIn + bio) per attendee; `0` = name + company only (faster) |
| `SKIP_CONTACTS` | `0` | Skip the first N attendees — use to resume an interrupted run |
| `MAX_CONTACTS` | `0` (unlimited) | Stop after N attendees — use for test batches |
| `ROW_INSERT_INTERVAL` | `0.3` | Seconds to wait between Notion row writes |
| `PAGE_DELAY_MIN` | `1.5` | Minimum seconds between Swapcard pagination requests |
| `PAGE_DELAY_MAX` | `4.0` | Maximum seconds between Swapcard pagination requests |
| `RUN_MODE` | (menu) | `scraper`, `enricher`, `evaluator`, `website`, `bot` — skips the interactive menu |

### LinkedIn Enricher Controls

| Variable | Default | Description |
|---|---|---|
| `SEARCH_BACKEND` | `ddg` | Search backend: `ddg` (DuckDuckGo, free), `serper` (paid, reliable), `gemini` (uses Gemini API) |
| `MAX_LOOKUPS` | `300` | Maximum searches per enricher run; `0` = unlimited |
| `SEARCH_INTERVAL` | `5.0` | Seconds between DuckDuckGo searches |
| `SEARCH_JITTER` | `0.4` | Randomise each wait ±40% to avoid mechanical cadence |
| `SEARCH_COOLDOWN` | `30` | Base cooldown seconds after search failures (DDG) |
| `SEARCH_COOLDOWN_MAX` | `180` | Maximum cooldown seconds (DDG) |
| `SEARCH_COOLDOWN_AFTER` | `2` | Number of consecutive failures before cooldown kicks in |
| `SEARCH_BURST_SIZE` | `40` | Take a proactive break every N searches (DDG); `0` = disabled |
| `SEARCH_BURST_BREAK` | `300` | Seconds to pause for burst break (DDG) |
| `SERPER_SEARCH_INTERVAL` | `1.0` | Seconds between Serper searches |
| `GEMINI_SEARCH_INTERVAL` | `1.0` | Seconds between Gemini searches |
| `DEBUG_ENRICHER` | `0` | Set to `1` to print every query, result, and rejection reason |

### AI Evaluator Controls

| Variable | Default | Description |
|---|---|---|
| `MAX_EVALUATIONS` | `0` (unlimited) | Cap how many companies to evaluate per run |
| `EVAL_INTERVAL` | `3.0` | Seconds between company evaluations |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model for evaluations and bot scoring |
| `GEMINI_SCOUT_MODEL` | `gemini-3.1-flash-lite` | Gemini model for badge OCR |
| `GEMINI_MAX_RETRIES` | `5` | Retry attempts on Gemini 429 / 503 errors |

### Retry / Resilience

| Variable | Default | Description |
|---|---|---|
| `NOTION_RETRY_WAIT` | `2.0` | Base wait seconds before retrying rate-limited Notion calls |
| `NOTION_MAX_RETRIES` | `5` | Maximum Notion retry attempts per request |
| `SWAPCARD_RETRY_WAIT` | `2.0` | Base wait seconds before retrying Swapcard failures |
| `SWAPCARD_MAX_RETRIES` | `5` | Maximum Swapcard retry attempts per request |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds for all external requests |

---

## 7. Module-by-Module Function Reference

### `config.py`

Central configuration module. Loads all environment variables from the `.env` file and exposes them as module-level constants. No secrets are hardcoded.

| Function | Description |
|---|---|
| `validate()` | Checks that `SWAPCARD_BEARER_TOKEN`, `NOTION_API_TOKEN`, and `NOTION_DATABASE_ID` are all set. Raises a clear `RuntimeError` listing any missing secrets rather than silently failing deep in a run. |

---

### `main.py`

The core scraper controller and interactive CLI entry point. Orchestrates the full Swapcard → Notion pipeline.

| Function | Description |
|---|---|
| `_swapcard_headers()` | Builds the HTTP headers for Swapcard API requests. Tolerates a `SWAPCARD_BEARER_TOKEN` value that already includes the `"Bearer "` prefix (strips it to avoid sending it twice). |
| `_swapcard_post(payload, context)` | Posts a batched GraphQL request to Swapcard with automatic retry. Retries `429` (rate limit) responses by honoring the `Retry-After` header; retries `5xx` and network timeouts with exponential backoff up to `SWAPCARD_MAX_RETRIES` attempts. Returns the first entry of the batch response (a dict). |
| `fetch_page(end_cursor)` | Fetches one page of attendees using Swapcard's persisted (APQ) query. Sends the operation name and SHA-256 hash instead of the full query text. Advances pagination via `end_cursor`. Returns the `people` connection object with `nodes`, `pageInfo`, and `totalCount`. |
| `extract_role(node)` | Finds the job title inside the attendee's `fields` array (a union type with varying key names). Falls back to the top-level `jobTitle` field if no matching custom field is found. Field names are compared case-insensitively against the configured list: `job title`, `jobtitle`, `title`, `position`, `role`. |
| `linkedin_url(profile)` | Normalises any LinkedIn value from Swapcard into a full `https://www.linkedin.com/in/...` URL. Handles bare handles (`"ramiyermiya"`), paths with `in/` prefix, bare domains, and already-full URLs. Returns `None` for empty input. |
| `extract_linkedin(social_networks)` | Iterates the `socialNetworks` array from a Swapcard node and returns the formatted LinkedIn URL for the first entry whose `type` is `"LINKEDIN"`. |
| `fetch_person_detail(person_id, user_id)` | Fetches a single attendee's full profile via the persisted detail query (`EventPersonDetailsQuery`). This second API call per person is what populates `jobTitle`, `socialNetworks`, and `biography` — fields not included in the list-page nodes. Uses the same retry logic as `_swapcard_post`. |
| `enrich_contact(contact, node)` | Calls `fetch_person_detail` and writes the results (role, LinkedIn, notes/biography) into the contact dict in place. Failures are non-fatal — the contact keeps its list-derived values so a single failed profile request never drops the row entirely. |
| `igb_url(person_id)` | Builds the direct iGB Live attendee profile URL by appending the Swapcard `person_id` to `IGB_LIVE_PERSON_BASE_URL`. |
| `map_node(node)` | Flattens a raw Swapcard attendee node into the flat contact dict that `notion_sync.sync_contact` expects: `{name, company, role, linkedin, notes, igb_url}`. |
| `run()` | Main scraper loop. Validates config, checks the Notion schema, then paginates through all Swapcard attendees. For each person: maps the node, optionally enriches the profile, syncs to Notion, and prints progress. Respects `SKIP_CONTACTS` (resume offset) and `MAX_CONTACTS` (test cap). Prints a summary of created/updated/skipped/error counts when done. |
| `_run_enricher_interactive()` | Interactive wrapper for the LinkedIn enricher. Prompts the user to choose a search backend (DuckDuckGo / Serper / Gemini) and optionally override `MAX_LOOKUPS`, then calls `linkedin_enricher.run()`. |
| `_run_evaluator_interactive()` | Interactive wrapper for the AI evaluator. Prompts for an optional `MAX_EVALUATIONS` cap, then calls `company_evaluator.run()`. |
| `_run_website_pass_interactive()` | Interactive wrapper for the website domain finder. Prompts for a batch cap, then calls `company_evaluator.run_website_pass()`. |
| `main()` | Entry point. Checks `RUN_MODE` for non-interactive operation; otherwise presents the five-option CLI menu and dispatches to the selected tool. |

---

### `notion_sync.py`

The Notion API layer. All communication with Notion goes through this module. Handles authentication, rate-limit retries, schema validation, deduplication, and payload construction.

| Function | Description |
|---|---|
| `_headers()` | Returns the Notion API request headers (Authorization, Notion-Version, Content-Type). |
| `_parse_retry_after(value)` | Parses a `Retry-After` header value (integer seconds) into a float. Returns `None` if the value cannot be parsed (e.g. HTTP-date format), so the caller falls back to the configured default instead of crashing. |
| `_notion_request(method, url, json_body, idempotent)` | Issues a Notion API call with automatic retries. Honors `Retry-After` on `429` responses. Retries `5xx` errors with exponential backoff, but only for idempotent calls — non-idempotent creates are not retried on `5xx` because the server may have already processed the request. Returns the final `requests.Response`. |
| `_rich_text(value)` | Converts a value into a Notion `rich_text` array. Coerces non-strings to `str`. Respects Notion's 2,000-character block limit (truncates silently). Returns an empty list for `None` or empty input. |
| `clear_schema_cache()` | Drops the cached database schema. Call this if you switch `NOTION_DATABASE_ID` mid-session. |
| `get_schema()` | Fetches and caches the target database's `{property_name: type}` map. Makes one API call on the first invocation; subsequent calls return the cached result. |
| `ensure_required_schema()` | Verifies that the `Name` property exists and is of type `title`. Raises `RuntimeError` and aborts the run if it is missing or the wrong type — without a title property, deduplication cannot work. |
| `report_schema_mismatches()` | Prints a one-time warning for every mapped property that either does not exist in the database or exists with the wrong type. Lets you know before a long run which fields will be silently skipped. |
| `build_properties(contact)` | Maps a flat contact dict to a Notion properties payload, skipping any field whose column is missing or the wrong type in the live database. This makes the writer resilient to schema drift — a renamed or retyped column silently skips that field instead of failing the whole row. |
| `find_existing(name)` | Queries the Notion database for a page whose `Name` equals the given string. Returns `(page_id, already_set)` where `already_set` is a dict of contact keys already populated in Notion (currently tracks `igb_url`). Returns `(None, {})` when no match is found. |
| `create_contact(contact)` | Creates a new Notion page in the target database with the given contact's properties. Marked non-idempotent so it is not retried blindly on `5xx`. |
| `update_contact(page_id, contact)` | Updates an existing Notion page (PATCH) with the given contact's properties. |
| `sync_contact(contact)` | The main entry point for writing a single contact. Checks whether the contact already exists (`find_existing`), updates if found, creates if not. Skips the write entirely if the `IGB URL` column is already populated (meaning the row was fully synced on a previous run). Applies the `ROW_INSERT_INTERVAL` rate-limit pause after every write. Returns `(status, note)` where status is `"created"`, `"updated"`, `"skipped"`, or `"error"`. |

---

### `linkedin_enricher.py`

Finds missing LinkedIn profile URLs for Notion contacts by searching DuckDuckGo, Serper.dev, or Gemini AI. Applies strict verification guards before writing any URL to Notion.

| Function | Description |
|---|---|
| `validate()` | Checks that `NOTION_API_TOKEN` and `NOTION_DATABASE_ID` are set, plus `SERPER_API_KEY` if `SEARCH_BACKEND=serper` or `GEMINI_API_KEY` if `SEARCH_BACKEND=gemini`. Raises a clear error for any missing secret. |
| `_strip_accents(text)` | Converts accented characters to their ASCII equivalents (e.g. `Kārlis` → `karlis`) for robust cross-language name matching. |
| `_name_tokens(name)` | Splits a person's full name into meaningful lowercase ASCII tokens, discarding name particles (de, van, bin, etc.) and single-character tokens. |
| `_company_variants(company)` | Splits a multi-company field (e.g. `"HHK Consulting / vip-grinders.com"` or `"Taptica (Nexxen)"`) into individual company names for separate searches. Returns the full string first, then each part — because Swapcard often packs two companies into one cell, and quoting the whole string rarely matches verbatim. |
| `_company_tokens(company, min_len)` | Extracts identifying tokens from a company name, discarding generic corporate suffixes (Ltd, Inc, Group, Consulting, etc.) and employment-status words (freelance, self-employed, consultant). Defaults to ≥4 characters; falls back to ≥2 for short-acronym employers like IBM or SAP. |
| `_token_present(token, haystack)` | Checks whether a token appears as a whole word in the haystack (word-boundary match). Prevents partial matches like `cisco` matching inside `francisco`. |
| `_company_corroborated(company, title, body)` | Returns `True` if at least one identifying company token actually appears in the search result's title or snippet. This is the core guard against same-name/wrong-company matches — DuckDuckGo only loosely honors quoted company names, so the company must surface in the result itself, not just in the query. |
| `name_matches_profile(name, href, title)` | Returns `True` only if both the first and last name tokens appear in the LinkedIn profile's `/in/` slug or result title. Deliberately ignores the result snippet/body, because that text echoes the searched company and could falsely match a different person with the same given name. |
| `_paced_sleep()` | Waits between searches using the active backend's configured interval and jitter. DDG uses a slow, heavily jittered pace (default 5s ±40%); Serper and Gemini use a lighter 1s pause. |
| `_is_transient_search_error(exc)` | Inspects an exception to determine whether it is a transient failure (rate limit, timeout, network error) that should trigger adaptive backoff and a later retry, versus a permanent failure that should mark the row as Skipped. |
| `_search_cooldown_seconds(consecutive_errors)` | Calculates the adaptive backoff pause after consecutive search failures. The pause escalates with each consecutive error and is capped at `SEARCH_COOLDOWN_MAX`. Resets to zero after a successful search. Uses different cooldown settings per backend. |
| `_maybe_search_cooldown(consecutive_errors)` | Prints a log line and sleeps for the calculated cooldown, if any. |
| `_ddg_text(query)` | Runs a single DuckDuckGo text search via the `ddgs` library. Returns the result list or `None` for a genuine "no results". Re-raises rate-limit and timeout exceptions so the caller can trigger backoff. |
| `_serper_text(query)` | Runs a single Serper.dev Google search and normalises the response to the same `[{href, title, body}]` format as `_ddg_text`. Raises `RatelimitException` on HTTP 429. |
| `_gemini_search(query)` | Uses Gemini with Google Search grounding to find LinkedIn profiles. Sends a natural-language prompt, extracts real URLs from grounding metadata chunks, and falls back to scanning Gemini's text for any LinkedIn URLs it cited directly. Returns results in the same `[{href, title, body}]` format. |
| `_search_text(query)` | Dispatches to the configured backend: `_ddg_text`, `_serper_text`, or `_gemini_search`. |
| `search_linkedin(name, company)` | The main LinkedIn search function. Builds several queries in order of confidence (full company string, then each sub-company, then name-only as last resort) and returns the first `/in/` URL that passes both `name_matches_profile` and `_company_corroborated` checks. Returns `(url_or_None, queries_run)`. The `queries_run` count lets the caller charge all searches against `MAX_LOOKUPS`. |
| `_plain_text(prop)` | Extracts plain text from a Notion title or rich_text property. |
| `_read_linkedin(props)` | Reads the current LinkedIn value from a Notion page's properties, handling both `url` and `rich_text` column types. |
| `_read_status_names(props)` | Reads the current enricher status column values (`Yes`, `Skipped`, etc.) from a row, handling `multi_select`, `select`, and `status` column types. |
| `_linkedin_is_empty_filter()` | Builds the Notion filter for "LinkedIn is empty" that matches the column's actual type in the database (url or rich_text). |
| `_build_query_filter()` | Combines the "LinkedIn is empty" filter with a "status column is also empty" filter so rows already stamped `Yes`, `No`, or `Skipped` are excluded from future runs — this is what makes the `Skipped` stamp sticky. |
| `_build_prefilled_linkedin_filter()` | Builds a filter for rows that already have a LinkedIn URL but no enricher status stamp (e.g. filled by the scraper). Used by `backfill_linkedin_status`. |
| `_stamp_status(page_id, option_name)` | Writes only the enricher status column to a Notion page (e.g. `"Yes"` or `"Skipped"`), leaving all other columns untouched. |
| `_status_property(option_name)` | Builds the Notion property payload for the status column, handling `multi_select`, `select`, and `status` column types. |
| `backfill_linkedin_status()` | Stamps `"Yes"` on every Notion row that already has a LinkedIn URL (e.g. filled by the scraper) but no enricher status. Prevents those rows from appearing in enricher queries and causing confusion. Returns the number of rows stamped. |
| `fetch_contacts_missing_linkedin()` | Pages through the entire Notion database and returns every row where LinkedIn is empty and the status column is also empty (not yet attempted). Returns a list of `{page_id, name, company, linkedin}` dicts. |
| `run()` | Main enricher loop. Validates config, optionally backfills status for pre-filled rows, fetches all rows needing LinkedIn, then for each: runs `search_linkedin`, writes the verified URL to Notion on success, or stamps `Skipped` on failure. Applies the per-search rate-limit pace, burst breaks, and adaptive backoff. Stops early if `MAX_LOOKUPS` is reached. Prints a summary at the end. |

---

### `company_evaluator.py`

Qualifies iGB Live leads using DuckDuckGo web snippets and Gemini AI. Groups contacts by company so each company is evaluated only once, then applies the result to every person at that company.

| Function | Description |
|---|---|
| `validate()` | Checks that `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`, and `GEMINI_API_KEY` are all set. |
| `_plain_text(prop)` | Extracts plain text from a Notion title or rich_text property. |
| `_rich_text_value(prop)` | Reads a rich_text column value as a plain string. |
| `_build_filter()` | Builds the Notion query filter that selects rows with a blank or `"Error"` AI Evaluation column AND a non-empty Company column. Error rows are retried so a transient Gemini failure doesn't permanently skip a lead. |
| `fetch_needs_website()` | Returns rows where `AI Evaluation = Done` but `Website Evaluator` is blank — companies that were evaluated before the website-finding feature existed, or where the website pass previously failed. |
| `fetch_unevaluated()` | Pages through the entire Notion database and returns all rows needing evaluation (blank or `"Error"` AI Evaluation). Also reads any cached `AI Web Context` snippet so Error retries can skip a repeated DuckDuckGo search. |
| `_rt(text)` | Builds a Notion `rich_text` property value (truncated at 2,000 chars). |
| `_title_rt(text)` | Builds a Notion `title` property value (truncated at 2,000 chars). |
| `_patch_page(page_id, properties)` | Issues a PATCH request to update a Notion page's properties. |
| `_write_snippet(page_id, snippet)` | Saves the raw DuckDuckGo snippet to the `AI Web Context` column **before** calling Gemini. This preserves the snippet if Gemini fails — a human can read it manually, and the next automated retry can reuse it without repeating the search. |
| `_write_website(page_id, domain, status)` | Writes the official website domain (as a full URL) to the `Website Domain` column and stamps the `Website Evaluator` column. Handles both `url` and `rich_text` column types. |
| `_write_result(page_id, company_type, score, rationale, status, snippet)` | Writes all AI evaluation columns to a Notion page: `AI Category`, `AI Score` (as `"4/5"`), `AI Rationale`, `AI Evaluation` (status), and optionally `AI Web Context`. Handles both `rich_text` and `title` column types. Logs a loud warning if the gate column (`AI Evaluation`) can't be written — without the `"Done"` stamp the row will be re-evaluated on every future run. |
| `_results_to_snippet(results)` | Flattens up to 3 search result dicts (title + body) into a single string capped at 800 characters, used as Gemini's web context. |
| `_results_to_urls(results)` | Extracts up to 5 candidate page URLs from search results to send to Gemini for website domain identification. |
| `_ddg_search(query)` | Runs a single DuckDuckGo search for company context (up to 5 results). Returns an empty list on any failure. |
| `_serper_search(query)` | Runs a Serper.dev search as a fallback when DuckDuckGo fails. Returns an empty list if `SERPER_API_KEY` is not set or the request fails. |
| `_snippet_mentions_company(snippet, company)` | Guards against DuckDuckGo returning a generic iGaming result (e.g. GLI) for an unrelated company search. Returns `True` only if at least one meaningful token from the company name appears in the snippet. |
| `_search_company(company)` | Fetches web context for a company name using a four-attempt strategy: (1) DuckDuckGo iGaming-flavoured query, (2) DuckDuckGo plain name, (3) Serper iGaming query, (4) Serper plain name. Validates that the company name appears in each snippet before accepting it. Returns `(snippet, candidate_urls)` or `("", [])` if all attempts fail. |
| `_gemini_client()` | Creates a Gemini API client using `GEMINI_API_KEY`, temporarily shadowing `GOOGLE_API_KEY` to prevent the SDK from routing to the wrong quota project. |
| `_parse_retry_delay(exc)` | Extracts the `retryDelay` seconds from a Gemini 429 error body (e.g. `retryDelay: '21s'`). Returns `None` when no delay is found so the caller can apply an escalating fallback. |
| `_is_gemini_rate_limit(exc)` | Returns `True` for transient Gemini errors worth retrying: HTTP 429 (`RESOURCE_EXHAUSTED`) and 503 (`UNAVAILABLE`). |
| `_call_gemini(client, user_message)` | Sends a message to Gemini with the evaluator system prompt. Automatically retries on 429/503, waiting exactly as long as Gemini requests in its `retryDelay` field, with a small random jitter to prevent synchronized retries. Raises after `GEMINI_MAX_RETRIES` exhausted attempts. |
| `assess_company(client, company, role, snippet, candidate_urls)` | The main Gemini evaluation call. Sends the company name, contact role, web snippet, and candidate URLs to Gemini; parses the JSON response; and returns a dict with `company_type`, `score` (1–5), `rationale`, and `website_domain`. |
| `find_website_domain(client, company, snippet, candidate_urls)` | A lightweight Gemini call using only the website-identification prompt (no scoring/rationale) to find the company's official domain from the candidate URL list. Used by the standalone website pass. |
| `_assess_website_only(client, company, cached_snippet)` | Runs `_search_company` (or reuses a cached snippet) and then calls `find_website_domain`. Returns `(domain_or_empty_string, snippet_used)`. |
| `run_website_pass()` | Finds all rows where `AI Evaluation = Done` but `Website Evaluator` is blank, then for each company: reuses the cached snippet if available (skipping DuckDuckGo), calls Gemini to identify the domain, and writes the result. Groups rows by company to evaluate each only once. |
| `run()` | Main evaluator loop. Validates config, fetches all unevaluated rows, groups them by company name, then for each company: searches DuckDuckGo for context, saves the snippet to Notion, calls Gemini, and writes all four AI columns plus the website domain to every contact at that company. Skips rows with no company name. Respects `MAX_EVALUATIONS`. Prints a summary at the end. |

---

### `telegram_bot.py`

The 24/7 Conference Scout bot. Handles two types of input: text messages (name or company search) and photos (conference badge scanning).

**Startup assets (loaded once at import time):**

| Function | Description |
|---|---|
| `_load_my_profile()` | Reads `my_profile.md` from the bot's directory. Returns an empty string if the file is not found. The profile is used by Gemini to generate personalised opening lines for contacts. |
| `_load_connections()` | Reads a LinkedIn Connections CSV export and returns a lowercase set of full names. LinkedIn prepends 3 note lines before the real header, so the loader scans for the first line containing both `"First Name"` and `"Last Name"`. Used to alert the user when a scanned contact is already in their network. |

**Gemini functions:**

| Function | Description |
|---|---|
| `_gemini_client()` | Creates a Gemini API client. Raises clearly if `GEMINI_API_KEY` is not set. |
| `_strip_fences(text)` | Strips Markdown code fences (` ```json ... ``` `) from Gemini's response text before JSON parsing. |
| `gemini_score_grounded(client, name, company, role)` | Real-time lead evaluation using Gemini with **Google Search grounding**. Searches the web for the company and produces `company_type`, `score`, and `rationale`. Used when a contact is not found in Notion. |
| `gemini_intel_brief(client, contact)` | Produces a live strategic intelligence brief for a contact using Gemini Google Search grounding. Returns `bullets` (3–4 dense factual statements with source URLs) and `photo_url` (a page where the person's face is visible). Called for every successful Notion lookup and every Gemini fallback. |
| `gemini_ocr_badge(client, image_bytes)` | Passes a conference badge photo to Gemini multimodal. Extracts `name`, `company`, `role`, and `raw` (full verbatim text). Used when the user sends a photo instead of typing a name. |

**Notion search helpers:**

| Function | Description |
|---|---|
| `_plain_text(prop)` | Extracts plain text from a Notion title or rich_text property. |
| `_url_val(prop)` | Extracts a URL string from a Notion url property. |
| `_select_val(prop)` | Extracts option name(s) from a Notion multi_select, select, or status property. |
| `_extract_page(page)` | Flattens a raw Notion page object into a flat contact dict with keys: `page_id`, `name`, `company`, `role`, `linkedin`, `linkedin_status`, `ai_category`, `ai_score`, `ai_rationale`, `ai_eval`. |
| `_notion_query(body)` | Executes a Notion database query and returns a list of extracted contact dicts. |
| `search_by_name_exact(name)` | Queries Notion for contacts whose Name exactly matches the query string. Returns up to 5 results. |
| `search_by_name_fuzzy(name)` | Queries Notion for contacts whose Name contains the query string. Returns up to 10 results. |
| `search_by_company(company)` | Queries Notion for contacts whose Company field contains the query string. Returns up to 25 results. |

**LinkedIn connection check:**

| Function | Description |
|---|---|
| `is_connected(name)` | Returns `True` if the person's lowercased full name is in the set loaded from `Connections.csv`. Triggers a `⚠️ NETWORK HIT` warning in the bot's reply. |

**Message formatting:**

| Function | Description |
|---|---|
| `_e(s)` | HTML-escapes a string for safe inclusion in Telegram HTML messages. |
| `_score_emoji(score_raw)` | Maps a numeric score to an emoji: 🔥 (5), 🟢 (4), 🟡 (3), 🟠 (2), 🔴 (1), ⚪ (unknown). |
| `_linkedin_line(contact)` | Formats the LinkedIn section of a profile card: a clickable link if the URL is known, `"not found (searched)"` if the enricher already tried and gave up, or `"not in database"` otherwise. |
| `format_single_profile(contact, intel, connected)` | Formats the full profile card for a single contact: name, company, role, AI score + emoji + rationale, LinkedIn link, profile photo link, and intelligence bullets. Prepends a network-hit warning if `connected = True`. |
| `format_company_results(query, contacts)` | Formats a company search result showing the best AI evaluation (highest score) and a list of all contacts at that company with LinkedIn and network indicators. Ends with a prompt to reply with a full name for the complete profile. |
| `format_gemini_fallback(name, company, role, eval_result, intel, connected)` | Formats the response when the contact is not found in Notion. Shows the live Gemini score and intelligence brief with a `"Not in Notion — live Gemini web search"` header. |

**Search pipeline:**

| Function | Description |
|---|---|
| `search_pipeline(query, client)` | The main text-search flow. Tries in order: (1) exact name match in Notion → single profile card; (2) company contains match → profile card or contact list; (3) fuzzy name contains → profile card or list; (4) nothing found → Gemini Search Grounding fallback. For every Notion hit, also runs `gemini_intel_brief` to append live news. |
| `badge_search_pipeline(name, company, role, client)` | Same search pipeline but with pre-extracted name/company/role from OCR. Used after a badge photo is scanned. |

**Telegram handlers:**

| Function | Description |
|---|---|
| `handle_text(update, context)` | Async handler for text messages. Sends a `typing...` indicator, runs `search_pipeline` in a thread executor (to avoid blocking the async event loop), and replies with the formatted HTML result. |
| `handle_photo(update, context)` | Async handler for photo messages. Downloads the image, runs `gemini_ocr_badge` to extract badge text, sends an intermediate "scanning" message, then runs `badge_search_pipeline` with the extracted data and replies with the result. |
| `run()` | Starts the Telegram bot. Validates all required env vars, sets up logging, registers the photo and text handlers, and starts the polling loop. |

---

## 8. Data Files

These two files are read at bot startup and mounted as Docker volumes so they can be updated without rebuilding the image.

### `my_profile.md`
Your professional background in Markdown format. The bot passes this to Gemini when generating personalised opening-line suggestions for contacts. Place this file in the repo root on your VPS. The bot runs without it — opening lines are simply disabled.

**Example content:**
```markdown
# About Me
I am the CEO of BlocksRace, a B2B iGaming company providing innovative betting markets.
I focus on partnerships with casino operators and sportsbooks.
Background: 10 years in iGaming, previously at [Company].
Looking to: expand into new markets, sign content deals with operators.
```

### `Connections.csv`
Your LinkedIn connections export. Used to flag contacts you already know. Export from: LinkedIn → My Network → Connections → Manage → Export.

The file has 3 note lines before the real header row. The bot's loader skips them automatically by scanning for the first line containing both `"First Name"` and `"Last Name"`.

---

## 9. Docker Services Summary

| Service | Starts automatically | `RUN_MODE` | Required secrets |
|---|---|---|---|
| `bot` | Yes (always-on, `restart: unless-stopped`) | `bot` | `TELEGRAM_BOT_TOKEN`, `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`, `GEMINI_API_KEY` |
| `scraper` | No (run manually) | `scraper` | `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`, `SWAPCARD_BEARER_TOKEN`, `SWAPCARD_COOKIE` |
| `enricher` | No (run manually) | `enricher` | `NOTION_API_TOKEN`, `NOTION_DATABASE_ID` (+ `SERPER_API_KEY` or `GEMINI_API_KEY` for non-DDG backends) |
| `evaluator` | No (run manually) | `evaluator` | `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`, `GEMINI_API_KEY` |

The `scraper`, `enricher`, and `evaluator` services use the `jobs` Docker profile so they never start automatically alongside the bot.

---

## 10. Troubleshooting

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
