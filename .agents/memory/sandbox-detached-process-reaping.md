---
name: Detached processes get reaped in this sandbox
description: Why long-running background scripts die mid-run, and how to run them
---

# Background/detached processes are killed when the launching tool call returns

In this Replit agent sandbox, a process started in the background from a bash
tool call (`&`, `nohup ... &`, even `setsid ... &`) is terminated shortly after
the launching command returns. Symptoms: the process is alive for the few
seconds of the launching call, then later polls show it gone, with a log file
that stops partway and **no traceback** (signal-killed, output buffer not
flushed).

**Why it matters:** Long jobs (e.g. a multi-thousand-row API sync that takes
~90 min) cannot be completed via agent bash calls — each call also caps at ~120s.
Detaching does not survive.

**How to apply:**
- For verification, run a **capped batch** that fits in one ~110s call
  (`timeout 110 ...` plus a row/limit cap), and verify results against the
  destination system (e.g. query the DB) rather than trusting buffered logs.
- For the real long run, hand it to the **user to run in the Replit Shell**
  (a persistent terminal that is not reaped), e.g. `cd <dir> && python main.py`.
- Make such scripts **idempotent** (dedup on a stable key) so partial/interrupted
  runs and re-runs are safe.
