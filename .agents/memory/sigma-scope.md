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
