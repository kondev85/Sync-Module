# ── Stage 1: dependency layer ─────────────────────────────────────────────────
# This layer is cached as long as requirements.txt doesn't change,
# so rebuilds after source-only edits are near-instant.
FROM python:3.12-slim AS deps

WORKDIR /build

RUN pip install --upgrade pip --no-cache-dir

COPY swapcard_sync/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Copy installed packages from the deps stage (keeps image lean)
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

WORKDIR /app/swapcard_sync

# Copy source — this layer rebuilds only when code changes
COPY swapcard_sync/ ./

# Default: run the Telegram Conference Scout bot.
# Override by passing a different RUN_MODE environment variable:
#   RUN_MODE=scraper   → Swapcard scraper & Notion sync
#   RUN_MODE=enricher  → LinkedIn URL enricher
#   RUN_MODE=evaluator → AI company evaluator
#   RUN_MODE=bot       → Telegram Conference Scout (default)
ENV RUN_MODE=bot

# Unbuffered output so logs appear immediately in docker logs / Hetzner console
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "main.py"]
