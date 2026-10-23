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
8. [SiGMA World Attendee Importer](#8-sigma-world-attendee-importer)

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
| `Source1` | Multi-select | Optional | Adds `IGB2026` for Swapcard/iGB; retains existing tags |
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
  [6] Run SiGMA World Attendee Importer
------------------------------------------------------------
```

For non-interactive / scripted runs, set `RUN_MODE` to skip the menu:

```bash
RUN_MODE=scraper  python -u main.py   # runs the scraper
RUN_MODE=enricher python -u main.py   # runs the LinkedIn enricher
RUN_MODE=evaluator python -u main.py  # runs the AI evaluator
RUN_MODE=website   python -u main.py  # runs the website domain finder
RUN_MODE=bot       python -u main.py  # starts the Telegram bot
RUN_MODE=sigma     python -u main.py  # SiGMA importer (defaults to a 20-contact cap)
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
| `SIGMA_COOKIE` | SiGMA importer only | — | Full Cookie header from a logged-in SiGMA Match browser session; keep secret |


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

---

## 8. SiGMA World Attendee Importer

The separate `sigma_sync/` package imports attendees from **SiGMA Match** into
the same Notion Lead Pool. It reuses the existing configuration and authenticated,
retrying Notion client. It does **not** change the Swapcard/iGB importer, run AI
evaluation automatically, or send messages or connection requests on SiGMA.
All SiGMA requests are read-only `GET` requests.

### Credentials and access

Only these three secrets are needed for this import:

| Secret | Purpose |
|---|---|
| `SIGMA_COOKIE` | Full `Cookie` request header copied from your logged-in browser at `https://match.sigma.world` |
| `NOTION_API_TOKEN` | Existing Notion integration token with read/update/insert access to the Lead Pool |
| `NOTION_DATABASE_ID` | Existing Lead Pool database ID |

To obtain the cookie: sign in to SiGMA Match, open your browser's developer tools,
select **Network**, open the attendee list, select its `/attendees?page=...`
request, and copy the **Cookie request-header value**. Save it as `SIGMA_COOKIE`
in Replit Secrets or your local root `.env`. Do not share it in chat, commit it,
or put it in a command where it will remain in shell history.
No separate SiGMA API key, Gemini key, or search key is required.
The importer URL-decodes the `XSRF-TOKEN` cookie for `X-XSRF-TOKEN` and sends
`X-Requested-With: XMLHttpRequest`. A 401/419 or login redirect stops the import
with **“SiGMA cookie expired, paste a new one”**.

### First test — 20 Operator attendees only

From the repository root, in a Linux/macOS terminal:

```bash
RUN_MODE=sigma MAX_CONTACTS=20 SIGMA_BUSINESS_TYPES=Operator python -u swapcard_sync/main.py
```

Or directly, without loading the evaluator/enricher:

```bash
MAX_CONTACTS=20 SIGMA_BUSINESS_TYPES=Operator python -u -m sigma_sync
```

In **VS Code on Windows**, open the repository folder, install the Python
extension, choose your Python interpreter, install the existing requirements
(including `python-dotenv` if not already installed), and use PowerShell:

```powershell
python -m pip install -r swapcard_sync/requirements.txt python-dotenv
$env:RUN_MODE = "sigma"
$env:MAX_CONTACTS = "20"
$env:SIGMA_BUSINESS_TYPES = "Operator"
python -u swapcard_sync/main.py
```

The capped run **writes to the real Notion database**, not a dry run. It prints a
per-attendee table and totals for `created`, `updated`, `skipped`,
`check-duplicate`, and `error`, then exits. A `check-duplicate` row is a newly
created row flagged for manual review; it is not also counted under `created`.
The importer does not automatically start a full run, the enricher, or evaluator.

| Variable | Default | Meaning |
|---|---|---|
| `RUN_MODE` | Menu | Use `sigma` or menu option **6** |
| `SIGMA_BUSINESS_TYPES` | Empty = all | Case-insensitive comma-separated business types, e.g. `Operator,Fintech`; a match on any type qualifies |
| `SIGMA_PIPELINE_CHECK` | **On (`1`)** | Check cached Pipeline Contacts, add `SiGMA2026` to Source, and append a one-time Lead Pool note; `0`, `false`, `no`, or `off` disables the entire check |
| `MAX_CONTACTS` | **20 for SiGMA when unset/empty** | Maximum qualifying attendees attempted, including already-imported skips. Explicit `0` removes the cap; use only after approving a full import |
| `SKIP_CONTACTS` | `0` | Offset in the **unfiltered source list**, not a count of Operators; use printed source indexes to resume |
| `PAGE_DELAY_MIN` / `PAGE_DELAY_MAX` | `1.5` / `4.0` seconds | Random delay between SiGMA requests, including profile requests |
| `ROW_INSERT_INTERVAL` | `0.3` seconds | Notion pagination/write pacing |
| `SWAPCARD_MAX_RETRIES` / `SWAPCARD_RETRY_WAIT` | `5` / `2.0` seconds | Existing shared retry settings reused for SiGMA GETs; exponential backoff and numeric `Retry-After` support |
| `NOTION_MAX_RETRIES` / `NOTION_RETRY_WAIT` | `5` / `2.0` seconds | Existing Notion retry settings; non-idempotent creates are not retried on 5xx |

The list uses 20 attendees per page and the current `totalUserCount` rather than
a hard-coded event size. Filtering happens **before** profile requests. It may
read several list pages to reach 20 Operators. With Pipeline checking disabled,
already-imported source URLs do not need another profile request. With the
default Pipeline check enabled, their profiles are still read for matching.
Profile retrieval first requests JSON; the
parser also handles SiGMA's HTML-embedded Inertia attendee data and conventional
labelled HTML profiles. Unexpected profile shapes stop the run instead of
silently creating incomplete rows.

### Notion columns and mapping

Create the following columns yourself if they do not already exist. Names are
case-sensitive. The importer **never changes the database schema**; it prints
missing/wrong-type columns and skips writing them. `Name` must be a Title column.
The Lead Pool uses uppercase **`SIGMA URL`**. The importer also accepts the
mixed-case `SiGMA URL` from the original specification; if both exist, it writes
to `SIGMA URL` and checks both for previously imported source URLs.

| Column | Type | SiGMA value |
|---|---|---|
| `Name` | Title | `first_name last_name`, retaining the supplied surname initial |
| `Role` | Text | `job_title` |
| `Company` | Text | `company` |
| `LinkedIn` | URL | Profile LinkedIn link |
| `Website Domain` | URL | Profile website (adds `https://` when necessary) |
| `Email` | Email or Text | Email only when it appears in the biography; never taken from account/session fields |
| `Notes` | Text | Biography (including booth/Telegram mentions), explicit booth/Telegram fields, What I Provide, I'm Looking For, provide/looking-for product/service categories, continents, languages |
| `SIGMA URL` | **URL** | `https://match.sigma.world/attendees/{uuid}`; strongly recommended for safe repeat imports |
| `Source1` | **Multi-select** | Adds `SiGMA2026`, retaining all existing tags |
| `Business Type` | **Multi-select** | Business types from the list |
| `Status` | Multi-select, Select, Status, or Text | New rows: `New`; ambiguous duplicate candidates: `Check duplicate` |

For a Notion **Status**-type column, create the `New` and `Check duplicate` options
before running: Notion does not create new Status options through page writes.
Missing `SIGMA URL` (and the mixed-case fallback) disables the strongest repeat-import check. In particular,
an ambiguous duplicate row cannot be reliably recognized on a later run without
that column. Add it before larger imports.

### Duplicate rules and field protection

The importer reads **all pages** of the existing Notion database before writing,
then checks in this order:

1. **Same SiGMA URL:** skip the attendee entirely.
2. **Same LinkedIn URL:** update the matching row. URL comparison ignores
   `www.`, scheme, query strings, letter case, and trailing slashes.
3. **Same first name + surname initial + normalized company:** update a unique
   match. First names are case-insensitive. Company comparison strips punctuation
   and legal/generic words `ltd`, `limited`, `plc`, `inc`, `llc`, `group`, `gmbh`,
   `sa`, `srl`, `bv`, `co`. For example, **Oliver M. / Playtech** matches
   **Oliver Marshall / Playtech plc**.
4. **Multiple candidates:** do not choose a row. Create a row with
   `Status = Check duplicate` and candidate names in `Notes`. Multiple exact
   LinkedIn matches are also treated conservatively this way.
5. **No match:** create a row with `Status = New`.

Updates fetch the current row again and **only fill empty mapped fields**.
Existing names, full surnames, emails, notes, roles, websites, LinkedIn URLs,
and statuses are preserved. The only overwrite exceptions are
`SIGMA URL` and `Business Type`. The importer does not tick `iGBLive`, replace
`IGB URL`, or touch AI/enricher stamps. Updated and created rows are indexed
immediately for subsequent duplicate checks in the same run.

### Lead Pool source attribution (both importers)

`PROP_SOURCE = "Source1"` in `swapcard_sync/config.py` names the shared
**Multi-select** column. Swapcard adds `IGB2026`; SiGMA adds `SiGMA2026` on
created and updated rows. Updates read the latest row, retain all existing
options, and omit `Source1` from the write when the tag is already present.
If the column is missing or has another type, a warning is printed and the
source field is skipped without failing the import. Previously imported rows
that are skipped by the existing source-URL checks remain skipped; this is not
a historical backfill.

Neither importer writes `iGBLive` or `SiGMAWorld` anymore. No Notion columns
are created, deleted, or retyped; you may remove the old checkboxes manually.
The SiGMA summary includes **Source1 tags added**, counting successful writes
that added the missing tag (including new rows). This does not include
Pipeline tags: Pipeline Contacts continues using its separate `Source`
column and its existing matching and write rules, unchanged.

If a write fails, the import stops and prints its partial results; it does not
blindly retry a possibly successful creation. Review Notion before restarting
after a connection failure. Index-based resume assumes the source ordering has
not changed.

### Existing tools after the import

Run `RUN_MODE=enricher` or `RUN_MODE=evaluator` as before. Both operate on the
same Notion columns, without changes. An abbreviated surname can make the
enricher's existing identity checks less conclusive; review uncertain matches.
The importer never expands a surname initial by guessing.

### Pipeline membership check

With `SIGMA_PIPELINE_CHECK=1` (the default), the existing Notion integration must
also have access to **Pipeline Contacts** and **PipeLine Companies**. No additional
token or database-ID variable is needed: the importer discovers these exact
database titles, validates the columns, and loads both databases completely once
at startup. Matching uses in-memory indexes, not per-attendee Pipeline queries.
Make sure only one accessible database has each title.

Required Pipeline schema:

- Pipeline Contacts: `Name` (Title), `LinkedIn` (URL), `Company` (Relation to
  PipeLine Companies), `Source` (Multi-select with existing option `SiGMA2026`).
- PipeLine Companies: `Company` (Title).

Matching stops at the first rule with any results:

1. Same normalized LinkedIn URL (lowercase; ignore scheme, `www.`, query string,
   and trailing slash).
2. Same case-insensitive first name, surname initial, and normalized company
   title from the Company relation. It uses the same punctuation/legal-suffix
   normalization as the Lead Pool matcher.
3. Two or more matches under either rule: tag nobody, print
   `pipeline ambiguous: <names>`, and do not fall through to a weaker rule.

For a unique match, the **only Pipeline mutation** is adding `SiGMA2026` to
`Source`, retaining all existing options. An already-tagged match requires no
Pipeline write. The in-memory tag cache is updated after successful writes, so
repeated attendees do not trigger duplicate patches.
The importer never creates/deletes Pipeline contacts or companies, edits their
other fields, or changes any Pipeline database schema.

The matching Lead Pool row also gets a separate line,
`In Pipeline: <full Pipeline name>`, once only. This explicit append is the sole
exception to preserving filled Lead Pool Notes: existing text, links, and
formatting are retained. It also works for attendees skipped because their
`SIGMA URL` was already imported. Unique Pipeline matches are required before a
note is added; ambiguous matches never add a speculative Pipeline note.

With Pipeline enabled, already-imported attendees still have their SiGMA profile
read for LinkedIn-first matching. With it disabled, the original fast
source-URL skip remains unchanged. The summary includes **pipeline tagged**,
**pipeline already tagged**, and **pipeline ambiguous** counts.

Pipeline Source updates preserve the startup snapshot of existing tags. Avoid
editing Source tags concurrently during an import; the Notion API does not offer
an atomic add-to-multi-select operation.

Example capped follow-up test, keeping the Operator filter:

```bash
RUN_MODE=sigma MAX_CONTACTS=40 SIGMA_BUSINESS_TYPES=Operator SIGMA_PIPELINE_CHECK=1 python -u swapcard_sync/main.py
```

### Docker on your own VPS

The image includes the sibling importer. Rebuild it once after pulling these
changes, then run an explicit one-shot capped import:

```bash
docker compose build scraper
docker compose run --rm -e RUN_MODE=sigma -e SIGMA_COOKIE \
  -e MAX_CONTACTS=20 -e SIGMA_BUSINESS_TYPES=Operator scraper
```

For that command, `SIGMA_COOKIE` must already be exported in the host shell;
alternatively place it in the project's root `.env` and use
`docker compose run --rm --env-from-file .env -e RUN_MODE=sigma -e MAX_CONTACTS=20 -e SIGMA_BUSINESS_TYPES=Operator scraper`
if your Docker Compose version supports `--env-from-file`.
The existing bot and one-shot service defaults remain unchanged.

### Module/function guide

| Module | Functions/classes and purpose |
|---|---|
| `sigma_sync/__init__.py` | Exposes the existing sibling configuration/Notion modules without modifying them |
| `client.py` | `SigmaClient`: owns the browser session; `_xsrf_header()` decodes the current CSRF token; `get()` issues paced GET-only requests with retry/auth guards; `attendees()` validates paginated list responses; `profile()` retrieves and parses one profile; `close()` releases the session. `CookieExpired` marks an expired login |
| `profiles.py` | `ProfileHTML` extracts embedded attendee JSON or labelled HTML; `_attendee()` selects only known attendee paths, excluding the signed-in user's metadata; `parse_profile()` chooses JSON/Inertia/HTML parsing; `text()` flattens strings/lists; `_label()` normalizes section headings; `clean_url()` validates HTTP(S) links; `plain_html()` strips biography markup; `build_contact()` assembles the mapped fields and full Notes |
| `notion_import.py` | `value()` reads Notion values; `normalized_company()`, `name_key()`, and `url_key()` build duplicate keys; `property_name()` resolves the supported source-URL column spellings; `rich_text()` splits long text into Notion-compatible blocks; `build_properties()` applies schema and fill-empty rules; `NotionImporter._load()` paginates the CRM; `_index()` builds/rebuilds lookups; `already_imported()` checks source URLs; `sync()` resolves duplicates and writes the appropriate create/update |
| `importer.py` | `run()` validates secrets, applies limits/filtering, paginates attendees, stops on failure, and closes resources; `print_summary()` prints per-row results and counts even after an interrupted import |
| `pipeline.py` | `discover_databases()` resolves the two exact Pipeline database titles; `_paginate()` loads all startup pages; `PipelineCheck` validates/caches contacts and company titles, `_full_relation()` handles truncated company relations at startup, `match()` applies ordered in-memory rules, and `check()` only adds the missing Source option or logs ambiguity |
| `__main__.py` | Enables `python -m sigma_sync` and clear CLI failures |
| `swapcard_sync/main.py` | `_run_sigma()` loads the sibling package; `main()` adds `RUN_MODE=sigma` and menu option 6 without changing the existing modes |

Run the offline tests from the repository root:

```bash
python -m unittest discover -s tests -p 'test_sigma*.py' -v
```
