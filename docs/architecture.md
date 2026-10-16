# Architecture

## Components

```
 Browser / Home Screen app ──HTTPS──> Caddy on the Pi ──> FastAPI (web/app.py) ──> items.py (one write path)
                                                             │        └─────────> ask.py, extract.py ─┐
                                                             │ thread                                   │
 scheduler (07:00 check, 03:00 backup) ──> runner ──> planner, search ──> claude.py ──> Claude Code ───┘
                                             │        pricing, insights, report         (Agent SDK, plan token)
                                             └──> SQLite data/dealwatch.db
```

One process: the web app hosts the scheduler; checks run on a background thread and share `execute_run()` and a
process-wide lock. Everything that talks to Claude goes through `services/claude.py`.

## Data model (`models.py`)

- **Item**: what to watch. `priority` sets the check interval (need 3 days, stock up 7, wishlist 14, unless
  `check_interval_days` is set). `unit` and an offer's pack size give the per-unit price. `target_price` and
  `target_unit_price` are "definitely buy" thresholds. `snooze_until` (set by Bought it), `active`, `last_checked_at`.
- **Retailer**: bare `domain`, `status` enabled, disabled or blocked, optional `categories`, `sort_order`.
- **Run** (a price check): status (`running`, `success`, `partial`, `failed`), counters (batches, items,
  `web_lookups`), `plan_json` (batches, shops, budgets), `log_json` (what was searched and read per batch, errors,
  dropped offers, warnings). A check cut off by a restart is marked failed at the next start.
- **RunItem**: per (check, item): `found`, `not_found` or `error` with a note. Items with an error keep their old
  `last_checked_at`, so they are due again.
- **Observation**: one offer: shop, title, URL, price, regular price, pack quantity, unit, unit price, promotion,
  validity, confidence, evidence, plus the assessment (`baseline_price`, `discount_pct`, `under_target`, `is_deal`,
  `score`, `reasons`).
- **PricePoint**: one per (item, check) with offers: min, average, median and max per pack and per unit, the best
  offer, promotion and deal counts. The tracked series behind baselines, the Tracker and the exports.
- **Report**: markdown and HTML per check. **Setting**: key and value behind `settings.Settings`.
  **ChatMessage**: the Ask conversation.

## A price check (`services/runner.py`)

1. `build_plan` (planner): due items, sorted by priority, last check and id, grouped by category, chunked to
   `group_size`, sorted by urgency, capped at `max_groups_per_run` (the rest is deferred). Per batch: the enabled
   shops serving the category (allowlist mode) or the blocked shops (open mode), a budget of
   `min(max_searches_per_group, 2 per item + 1)` web lookups, and each item's baseline. The Run row is written at once.
2. Batches run one after another (D16). `ClaudeSearchClient.search_group` starts one Claude Code run with the
   batch as JSON, WebSearch and WebFetch held by a `WebPolicy` (D19), and one tool, `record_item` (D18). A reached
   plan limit or a failed sign-in stops the remaining batches.
3. `_persist_group`: drops offers whose URL is outside the batch's shops or from a blocked shop, stores the rest
   with unit prices, writes RunItems and updates `last_checked_at`. Items a failed batch did not get to are errors.
4. `_assess_run`: per offer, the baseline (`pricing.baseline_for_item`: median of the best price per check over the
   window, strictly before this check) and `pricing.assess_offer` give the deal flags, score and reasons.
5. `_record_price_points`, then `_finalize`: status and the report.

## Claude (`services/claude.py`)

`Claude.ask(text, system, model, effort, tools, photo, web, timeout, max_turns)` runs Claude Code once: the system
prompt from a temporary file, Claude Code's own tools off except WebSearch and WebFetch when a `WebPolicy` is given,
the app's tools served in process (`create_sdk_mcp_server`), `permission_mode="dontAsk"`, no settings sources, its
own empty config folder, no session kept, and an environment holding only the plan token. A photo travels as an
image block in the one user message. Rate-limit events with status `rejected` and a failed sign-in raise
`CannotRun`; other failures raise `Unavailable` with a sentence for the person. `tool.py` turns a typed Python
function with a docstring into a tool whose arguments are validated before it runs.

## Smart entry and Ask

- `services/extract.py`: `draft(claude, text, photo)` sends the words (and the photo, shrunk by `prepare_image`),
  allows a lookup on the shop of a link in the words, and parses one JSON object into `ExtractedItem`, which
  `to_draft()` turns into the review card's fields.
- `services/ask.py`: `reply()` stores both sides; `build_system` adds the date, a compact snapshot of every item with
  the best offer of its latest check, and the last 20 turns; `make_tools` offers get_item, latest_deals, add_item
  (refuses likely duplicates via `similar.find_similar`), update_item and item_action, all through `items.py`.

## Insights and charts

`services/insights.py` derives per-item statistics from price points on demand (basis, series, last and previous,
lowest ever, 30-day average, trend, volatility, cheapest shop, promotion share), the per-shop breakdown and the
overview. `web/charts.py` renders inline SVG; `static/tracker.js` adds the hover crosshair.

## Web (`web/app.py`, `security.py`)

Server-rendered Jinja pages, plain forms, a few enhancements in `static/app.js` (confirm prompts, busy state, the
mic, the remembered model picker, live similar items, the More menu, the service worker). `style.css` holds the
colour tokens for both themes and the parts every page is made of: list rows with a category tile, cards, badges,
the segmented switch, the phone tab bar. `_macros.html` has the one item form (`item_form`, for adding and editing)
and the shared bits (`tile`, `next_check`). Home reads `queries.current_deals` (Ask's latest_deals does too),
`insights.price_drops` for the last check and `planner.due_at` for when each item is up again; times show as
"today 07:00" through `report.friendly_time`. A page with a check running refreshes itself every 20 seconds. The content security policy allows
scripts from the app only, so there are no inline handlers. `security.install` adds the login, the same-origin check
for state-changing requests and the headers.

## Failure handling

A batch error marks its unrecorded items `error` (they stay due) and the check `partial`; no completed batch makes it
`failed`. An exception in the pipeline marks the check `failed` with the error. Only one check runs at a time.
