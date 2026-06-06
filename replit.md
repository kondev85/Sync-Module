# [Project name]

_Replace the heading above with the project's name, and this line with one sentence describing what this app does for users._

## Run & Operate

### Swapcard → Notion sync (the real app: `swapcard_sync/`)

Run from a **Shell tab** (foreground), not from the Agent — the Agent sandbox kills long-running processes after ~120s. A full ~5000-attendee run takes 1–2 hours.

`python -u main.py` now shows an interactive menu: **[1]** Scraper & Sync, **[2]** Missing LinkedIn Finder & Enricher. For scripted/non-interactive runs, set `RUN_MODE=scraper` or `RUN_MODE=enricher` to skip the menu (the run commands below all run interactively — just press `1`, or prepend `RUN_MODE=scraper`).

- Full run (recommended pacing): `cd swapcard_sync && ENRICH_PROFILES=1 ROW_INSERT_INTERVAL=0.6 python -u main.py`
- Resume after an interruption: add `SKIP_CONTACTS=<last index shown in logs minus ~20>` — a small overlap is safe (dedup updates, never duplicates) and avoids gaps if Swapcard's list order drifted.
- Finish with one final full pass (`SKIP_CONTACTS=0`, no `MAX_CONTACTS`) to catch any attendees missed at chunk boundaries; it only updates existing rows.
- Test a chunk: `SKIP_CONTACTS=0 MAX_CONTACTS=50 ENRICH_PROFILES=1 python -u main.py`
- Required secrets: `SWAPCARD_BEARER_TOKEN`, `SWAPCARD_COOKIE`, `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`.

Key env toggles (see `config.py`): `ENRICH_PROFILES` (Role/LinkedIn/Notes), `SKIP_CONTACTS` (resume offset), `MAX_CONTACTS` (cap), `ROW_INSERT_INTERVAL`, `PAGE_DELAY_MIN/MAX`.

Dedup is by **Name**: re-running never creates duplicates (it updates the existing row), so restarts and overlapping chunks are safe.

### LinkedIn enricher (`swapcard_sync/linkedin_enricher.py`)

Separate from the scraper but unified in the same project: finds Notion rows with an empty **LinkedIn** and fills them by searching **DuckDuckGo** for `"Name" "Company" site:linkedin.com/in/` (via the `ddgs` library). Only searches rows that have **both** Name and Company.

- Run it: menu option **[2]**, or `cd swapcard_sync && RUN_MODE=enricher python -u main.py`.
- Required secrets: `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`. **No Google/search API key needed** — DuckDuckGo needs no credentials or quota project. (The old `GOOGLE_API_KEY`/`GOOGLE_CSE_ID` path was dropped because the Custom Search JSON API kept returning 403 even after enabling.)
- Rate-limit guardrail: DuckDuckGo has no fixed daily quota but throttles bursts. Hard cap of `MAX_LOOKUPS` searches/run (default **300**); `SEARCH_INTERVAL` (default **2.5s**, falls back to legacy `GOOGLE_LOOKUP_INTERVAL`) paces requests. If you hit rate limits, raise `SEARCH_INTERVAL`. A name+company match is a best guess, not a verified identity.

### Scaffold commands (unused template — api-server/db)

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

_Populate as you build — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

_Describe the high-level user-facing capabilities of this app once they exist._

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
