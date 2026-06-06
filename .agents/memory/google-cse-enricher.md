---
name: LinkedIn enricher search backend
description: Why the enricher switched from Google CSE to DuckDuckGo, plus the Google CSE setup gotchas (kept for history)
---

**Current state:** the LinkedIn enricher uses **DuckDuckGo** (`ddgs` library) — no API key, no Cloud project, no quota. It just works in this environment. The Google CSE path below was abandoned after repeated 403s the user couldn't resolve even after enabling the API (the key's owning project never lined up with where the API was enabled). DuckDuckGo throttles bursts, so pace with `SEARCH_INTERVAL` (default 2.5s) and cap per run with `MAX_LOOKUPS`. Don't reach back for Google CSE unless DuckDuckGo stops working.

---

**Historical (Google CSE):** The LinkedIn enricher previously called the Custom Search **JSON API** (`https://www.googleapis.com/customsearch/v1`), which needs TWO independent things that having an API key alone does NOT give you:

1. **The Custom Search API must be ENABLED in the API key's Cloud project.** A valid key still returns HTTP 403 `"This project does not have the access to Custom Search JSON API"` until you enable it at console.cloud.google.com/apis/library/customsearch.googleapis.com (correct project selected). Propagation takes a few minutes.
2. **The Programmable Search Engine (`cx`/`GOOGLE_CSE_ID`) must have "Search the entire web" ON.** Default engines only search added sites, so `site:linkedin.com/in/` returns zero results — the script runs fine but finds nothing.

**Why:** these are account-side config, not code — the code was correct while the live smoke test 403'd.

**Also:** the `<script src="https://cse.google.com/cse.js?cx=...">` widget snippet is the embeddable JS search box, NOT the JSON API. Its `cx` is reusable as `GOOGLE_CSE_ID`, but the JSON API additionally requires a separate Cloud `GOOGLE_API_KEY`. Don't conflate the two.
