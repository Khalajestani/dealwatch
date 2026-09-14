# Decisions

Short records of choices that are not obvious from the code. Add a dated entry when you change direction.

**2026-09-14 · Python + FastAPI + SQLite, server-rendered UI.** One process, one file DB, no JS toolchain: easiest for a
personal tool to run for years and for an agent to modify. uv pins Python 3.12 (the `anthropic` SDK 1.x needs ≥3.10).

**2026-09-14 · Claude web search instead of scraping shops.** Retailer sites (bol, AH, Kruidvat...) have anti-bot
protection and change layouts; scraping is fragile and against most terms of use. The server-side `web_search` tool
gives current results with URLs and citations, and its `allowed_domains`/`blocked_domains` map 1:1 onto the retailer
config page. Trade-off: prices come from search snippets/pages the model reads, so they need evidence + confidence and a
price history to be trusted. Structured folder/API sources are the planned upgrade (see ROADMAP).

**2026-09-14 · One LLM call per batch of ~6 items, grouped by category.** Grouping amortises the prompt and lets one
domain allowlist fit all items in the call. Larger batches degrade extraction quality; 4-8 is the sweet spot.

**2026-09-14 · Structured outputs (`output_config.format`) rather than a client tool for JSON.** No second round-trip,
schema-guaranteed JSON, and it composes with the server-side search loop. A client tool would end the turn with
`tool_use` and force a mixed server/client tool flow.

**2026-09-14 · Default model `claude-opus-5`, effort `medium`.** Extraction quality matters more than tokens here: a
wrong pack size or a marketplace price is worse than a few cents of tokens. Sonnet 5 is offered in Settings for a
~2.5x cheaper token bill; the search fee is the same either way. Server-side fallbacks (`fallbacks: "default"`) are on
by default so a rare safety decline doesn't kill a batch.

**2026-09-14 · Deal = versus own history first.** Shop "van/voor" prices are marketing; the median of what we have seen
over 90 days is the honest baseline. First observations can only be judged by shop list price, target price or an
explicit promo with decent confidence, and the report says so.

**2026-09-14 · Per-unit price normalisation.** Household bulk packs are the point of stocking up; comparing €/roll,
€/wash, €/100ml makes 8-packs and 24-packs comparable and stops "bulk" from looking expensive.

**2026-09-14 · Allowlist mode by default.** Searching only enabled shops wastes fewer searches and keeps results
actionable. Open mode exists for discovery, with blocked shops excluded.

**2026-09-14 · Naive UTC timestamps, additive migrations, no auth.** Simplicity for a local single-user app. Revisit
if it ever becomes multi-user or hosted.

**2026-09-14 · A fake search client is a first-class citizen.** Tests, dry runs and demos never spend credits.

**2026-09-14 · Price points as the tracked series; baseline = median of the best price per check.** Raw observations
mix cheap and expensive shops, so their median made mediocre prices look like deals. One aggregate row per item per
run (min/avg/median/max, per unit, best offer) is what a person means by "the price that week"; the median of its
best price over 90 days is the honest "normal" price. Raw observations stay for auditing and per-shop stats.

**2026-09-14 · Direct sources run for every active item, every run, and never count as an LLM check.** Folder/API
sources are free, so denser tracking costs nothing; the LLM stays on its priority-driven cadence. Offers carry a
`source` column so LLM and structured data can be compared later.

**2026-09-14 · Charts are server-rendered SVG with a tiny hover script, no chart library.** Keeps the app offline-
capable and dependency-free; the design follows the data-viz method (validated two-colour pair, sparse labels,
legend, table fallback).
