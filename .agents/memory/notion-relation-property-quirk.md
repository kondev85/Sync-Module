---
name: Notion relation-property visibility quirk
description: Why a Notion property can be invisible to the API yet reject writes, and how to handle schema drift in sync code
---

# Notion relation properties are hidden from an integration that lacks linked-DB access

A relation property whose **linked database is not shared with the integration
token** is *omitted entirely* from `GET /v1/databases/{id}` (it won't appear in
`properties`), yet writing to it still fails with
`"<Prop> is expected to be relation."` and you **cannot** `PATCH` the database to
convert/redefine it ("Cannot update property <Prop>").

**Why it matters:** This looks like a contradiction — the schema read says the
property doesn't exist, but writes say it must be a relation. The cause is access
scope, not a stale cache. A CSV export from the Notion UI *will* show the column
(the UI sees it), which is the giveaway.

**How to apply:**
- To put plain text into such a column, the **user must change the property type
  to Text in the Notion UI** (one click: column header → Edit property → Type →
  Text). The API can't do it.
- Alternatively populate the relation properly: share the linked DB with the
  integration, get its `database_id`, find-or-create the related page, and set the
  relation by page id. Much heavier; only do this if linked records are required.
- **Make sync writers schema-aware:** fetch the target DB schema once, and only
  emit properties that exist with the expected type; warn (don't crash) on
  missing/mismatched ones, and hard-fail only on the required title property.
  This survives this quirk and ordinary schema drift (renames/retypes) gracefully.
