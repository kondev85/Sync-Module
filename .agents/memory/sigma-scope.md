---
name: SiGMA importer scope
description: User-required boundaries between the SiGMA importer and existing conference tools
---

Keep the SiGMA importer separate from the Swapcard/iGB flow. Reuse the existing
Notion/configuration patterns; the existing LinkedIn enricher and company
evaluator must continue working on imported rows without changing their behavior.

**Why:** The user explicitly requested adding SiGMA alongside the existing tools,
not replacing or changing the Swapcard/iGB workflow.

**How to apply:** Keep source-specific matching and field-protection rules in
the SiGMA importer. Changes to shared tools must not alter existing source behavior.

The Pipeline check may only add `SiGMA2026` to a matched contact's Source,
preserving existing tags. Never create/delete Pipeline records or change other
Pipeline fields. Ambiguous matches must tag nobody.

**Why:** The user explicitly limited Pipeline writes to source attribution.

**How to apply:** Keep Pipeline matching cached at startup and writes restricted
to the Source property. The one-time membership note belongs in Lead Pool, not
in Pipeline Contacts.
