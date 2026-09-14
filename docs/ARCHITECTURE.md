# Architecture

## Components

```
 ┌──────────┐   forms    ┌──────────────┐          ┌───────────────┐
 │ Browser  │ ─────────▶ │ FastAPI (web)│ ───────▶ │ runner        │──▶ report / notify
 └──────────┘            └──────────────┘  thread  │  planner      │
 ┌──────────┐            ┌──────────────┐          │  llm (Claude) │──▶ Anthropic Messages API
 │ cron/CLI │ ─────────▶ │ cli.py       │ ───────▶ │  pricing      │    + web_search tool
 └──────────┘            └──────────────┘          └───────┬───────┘
 ┌──────────┐            ┌──────────────┐                  │ SQLAlchemy
 │ launchd  │ ─────────▶ │ scheduler    │ ─────────────────┘
 └──────────┘            └──────────────┘          SQLite: data/dealwatch.db
```

One process: the web app hosts the scheduler; runs execute on a background thread; the CLI can run the same
pipeline without the web app. Everything shares `execute_run()` and a process-wide lock.

## Data model (`models.py`)

- **Item** - what to watch. `priority` drives the check interval (need 3d / stock_up 7d / wishlist 14d unless
  `check_interval_days` is set). `unit` + offer pack size → per-unit price. `target_price` / `target_unit_price` are
  "definitely buy" thresholds. `snooze_until` (set by *Bought it*), `active`, `last_checked_at`.
- **Retailer** - `domain` (bare), `status` enabled/disabled/blocked, optional `categories` tags, `sort_order`.
- **Run** - one execution: status (`running`, `success`, `partial`, `failed`, `planned` for dry runs), counters
  (batches, items, searches, tokens, cache tokens), `cost_usd` (estimate from real usage), `estimated_cost_usd`
  (planner), `plan_json` (batches/domains/budgets), `log_json` (queries per batch, errors, dropped offers, warnings).
- **RunItem** - per (run, item): `found` / `not_found` / `error` + note. Gives "not found" history and retry semantics
  (errored items keep their old `last_checked_at`, so they are due again next run).
- **Observation** - one offer seen: shop, title, url, price, list price, pack quantity, unit, unit price, promo type/text,
  validity, confidence, evidence, plus assessment fields (`baseline_price`, `discount_pct`, `under_target`,
  `is_deal`, `score`, `reasons`). This table is the price memory.
- **Report** - markdown + HTML per run. **Setting** - key/value store behind `settings.Settings`.

## Run lifecycle (`services/runner.py`)

1. `build_plan` (planner): due items → sorted by (priority rank, last checked, id) → grouped by category → chunked to
   `group_size` → sorted by urgency → capped at `max_groups_per_run` (rest = deferred). Per batch: retailers serving
   the category (allowlist mode) or the global blocklist (open mode), `max_uses = min(max_searches_per_group, 2n+1)`,
   per-item baselines, and a cost estimate. Run row is written immediately (`status=running`).
2. Batches run in a `ThreadPoolExecutor(parallel_groups)`. As each completes, `_persist_group` adds usage to the run,
   drops offers whose URL domain is outside the batch allowlist / inside the blocklist / from a blocked retailer,
   stores observations with computed unit prices, writes RunItems and updates `last_checked_at` (not on error).
3. `_assess_run`: per observation, baseline (median of comparable prices in the window, strictly before this run) and
   `pricing.assess_offer` → deal flags, score, reasons. `deal_count` counts items, not offers.
4. `_finalize`: status, cost estimate from real usage, `log_json`, report (DB + `data/reports/run-NNNN.md`),
   optional notification (failures are logged, never fail the run).

Dry runs stop after step 1 and store the plan as the report (`status=planned`).

## The LLM request (`services/llm.py`, `services/prompts.py`)

Per batch, one `messages.create` (via `beta.messages.create` when server-side fallbacks are enabled):

- `model` = `search_model` (default `claude-opus-5`), `max_tokens` 16k, `thinking: adaptive`, `output_config.effort`.
- `system`: static `SYSTEM_PROMPT` with `cache_control: ephemeral` (cache hits only once the prefix exceeds the model's
  minimum cacheable size; harmless otherwise).
- `tools`: `web_search_20260318` with `max_uses`, `allowed_domains` **or** `blocked_domains`, `user_location`
  (country/city/timezone from settings), `response_inclusion: excluded` (drops raw result blocks from the response).
- `output_config.format`: JSON schema `OUTPUT_SCHEMA` → `results[] { item_id, status, note, offers[] }`.
- User message: date, locale, search budget, shops, items (name/brand/hints/unit/bulk_ok/priority/targets/baseline).
- `stop_reason == "pause_turn"` → resend the user message plus the assistant content unchanged (max 6 times).
  `refusal` / `max_tokens` → batch error. Usage (`input_tokens`, `output_tokens`, cache read/write,
  `server_tool_use.web_search_requests`) is summed over continuations. Queries are read from `server_tool_use` blocks.
- The final text block is parsed (`extract_json`) and validated with pydantic (`GroupOutput`), which normalises domains,
  currency, promo type and clamps confidence.

## Cost controls

Allowlisted domains (fewer irrelevant results) · batches of several items per call · `max_uses` per batch ·
batch cap per run · per-priority intervals and snoozing · cached static prompt · trimmed responses ·
structured output (no retry round-trips for JSON) · effort setting · dry-run estimate · real usage recorded per run.

## Failure handling

Batch-level: API errors, refusals, unparsable JSON → the batch fails, its items are marked `error`, remain due, and
the run ends `partial`. Run-level exceptions mark the run `failed` with the error text. Notification errors are
logged in `log_json`. A second run cannot start while one is active.

## Security notes

Local single-user tool, no authentication - bind to 127.0.0.1 (default) and do not expose it. Model/web supplied
text is escaped before it reaches HTML (Jinja autoescape; `report.safe` neutralises tags and link syntax in markdown;
non-http URLs are not linked). Secrets only come from `.env` / environment.
