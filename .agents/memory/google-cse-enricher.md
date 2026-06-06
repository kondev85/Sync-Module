---
name: LinkedIn enricher search backend
description: Why the enricher switched from Google CSE to DuckDuckGo, plus the Google CSE setup gotchas (kept for history)
---

**Current state:** the LinkedIn enricher uses **DuckDuckGo** (`ddgs` library) — no API key, no Cloud project, no quota. It just works in this environment. The Google CSE path below was abandoned after repeated 403s the user couldn't resolve even after enabling the API (the key's owning project never lined up with where the API was enabled). Don't reach back for Google CSE unless DuckDuckGo stops working.

**DuckDuckGo is loose + flaky — two consequences that drove the design:**
1. It does NOT strictly honor `"quotes"`/`site:`, so a `site:linkedin.com/in/` query for an unindexed person returns an *unrelated* profile. Writing the top hit blindly produced ~44% wrong matches. Fix: a name-verification guard — accept a result only if BOTH first and last name tokens (accent-folded) appear in the profile's `/in/` slug OR result **title**. Crucially do NOT match against the result **body/snippet**: it echoes the searched company/name, so a different person at the same company passes on a surname that's only in the snippet (real miss: "Ahmet Bulent Zorlu" → profile "Ahmet S. Önal"). 
2. It throttles bursts and is non-deterministic: the *same* query can raise `DDGSException("No results found.")` one minute and return hits the next. So pace with `SEARCH_INTERVAL` (default 2.5s), cap with `MAX_LOOKUPS`, and treat `RatelimitException`/`TimeoutException` as transient (leave the row for retry) but a plain "no results" as a genuine no-match.

**Enricher idempotency contract:** a Notion multi_select status column (`"LinkedIn Enreacher"` — note the user's spelling; options Yes/No/Skipped) makes outcomes sticky. Found→stamp "Yes"+write URL; no confident match→stamp "Skipped"+leave URL empty. The fetch filter is `LinkedIn is_empty AND status is_empty`, so any stamped row (incl. human-set "No") is never re-searched. Rows missing Name/Company are left unstamped so they retry if data is added. **Why:** without the status stamp, "no-match" rows would be re-searched (and re-fail) every run forever.

---

**Historical (Google CSE):** The LinkedIn enricher previously called the Custom Search **JSON API** (`https://www.googleapis.com/customsearch/v1`), which needs TWO independent things that having an API key alone does NOT give you:

1. **The Custom Search API must be ENABLED in the API key's Cloud project.** A valid key still returns HTTP 403 `"This project does not have the access to Custom Search JSON API"` until you enable it at console.cloud.google.com/apis/library/customsearch.googleapis.com (correct project selected). Propagation takes a few minutes.
2. **The Programmable Search Engine (`cx`/`GOOGLE_CSE_ID`) must have "Search the entire web" ON.** Default engines only search added sites, so `site:linkedin.com/in/` returns zero results — the script runs fine but finds nothing.

**Why:** these are account-side config, not code — the code was correct while the live smoke test 403'd.

**Also:** the `<script src="https://cse.google.com/cse.js?cx=...">` widget snippet is the embeddable JS search box, NOT the JSON API. Its `cx` is reusable as `GOOGLE_CSE_ID`, but the JSON API additionally requires a separate Cloud `GOOGLE_API_KEY`. Don't conflate the two.
