---
name: Swapcard people API
description: How to query attendee/people data from api.swapcard.com/graphql for scraping/sync work
---

# Swapcard people list API

The Swapcard web app's people/attendee list (`api.swapcard.com/graphql`) cannot be
queried with a hand-written GraphQL document.

**Key constraints (discovered the hard way):**
- **Introspection is disabled** — `__schema` returns INTERNAL_SERVER_ERROR. You cannot discover the schema programmatically.
- **It uses persisted queries (APQ).** The browser sends only an `operationName` + `extensions.persistedQuery.sha256Hash`; the server resolves the stored query. Raw query text fails validation hard (e.g. "Cannot query field view on type Query", union fields rejected).
- The request is **batched**: the POST body is a JSON *array* of operations, and the response is a matching array.
- For the people list: `operationName="EventPeopleListViewConnectionQuery"`, variables `{viewId, endCursor}` (cursor var is `endCursor`, NOT `after`). Response path: `resp[0].data.view.people.{nodes,pageInfo,totalCount}`.
- The persisted-query hash **rotates** whenever Swapcard redeploys their client → keep it overridable via env and re-capture from the browser Network tab if you get `PersistedQueryNotFound`.

**How to apply:** Don't try to author/introspect the query. Have the user capture the real request payload from DevTools → Network (just `operationName`, `variables`, and `extensions.persistedQuery.sha256Hash` — never headers, which leak the Bearer token + cookie). Replay that exact shape and advance `endCursor`.

**Auth gotcha:** Users often paste the token *with* the `Bearer ` prefix already included; strip a leading `Bearer ` before building the `Authorization` header or you send `Bearer Bearer <token>` → 401 invalid_credentials.

**Data caveat:** The list view's node (`Core_PeopleViewFeaturedCard`) often has `jobTitle: null`, empty `fields`, and **no `socialNetworks` field at all** — so role and LinkedIn are frequently unavailable from the list query alone.
