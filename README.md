# Dealwatch

A small, local web app that keeps your household shopping list and wish list, and a few times a week asks
Claude (with web search) to find the current price and promotions for each item at the Dutch shops you allow.
It remembers every price it sees, works out what "normal" is per item, and reports only the offers that are
genuinely below normal, below your target price, or a real promotion - with links, evidence and a confidence score.

You decide what to buy. Dealwatch never orders anything.

## Honest assessment (read this first)

- **It is a research assistant, not a price-comparison engine.** Web search snippets can be stale or ambiguous
  (pack sizes, marketplace sellers, "vanaf" prices). Expect some noise, especially in the first weeks. Every reported
  price carries the source link, a short quote as evidence, and a confidence score - treat the report as a shortlist.
- **The value builds over time.** On the first run there is no history, so it can only spot shop-declared promotions
  and target-price hits. After a few runs the per-item baseline (median of what it has seen) makes "is this actually
  cheap?" reliable. Give it 4-6 weeks before judging it.
- **Where it pays off:** stocking up on consumables during 1+1 / 2e-halve-prijs weeks (the unit-price normalisation makes
  bulk packs comparable), and waiting for a target price on wish-list items (tools, appliances). Where it doesn't: items
  whose price barely moves - you will just get "no deal" rows.
- **Cost is small but not zero.** A run with ~25 items is roughly 4-5 API calls and 25-35 web searches: about $0.30-0.80
  with Claude Opus 5 (see *Cost model*). Two runs a week is a few euros a month. The dry-run button shows the estimate
  before you spend anything, and every run records what it actually cost.
- **Supermarket folders are the real gold mine** for household deals and they are structured data. Reading them directly
  (no LLM needed) is the top item on the roadmap; today they are only found when a search result surfaces them.
- **Runs only while the app runs.** It is a local tool: schedule it on a machine that is on at 07:00 (Mac with launchd, a
  NAS, a small VPS) or run it from cron. There is no cloud component and no login - do not expose it to the internet.

## How it works

1. **Plan** - pick the active items that are due (interval depends on priority: need / stock up / wishlist), order by
   urgency, group them by category and split into batches of ~6. Each batch gets the enabled retailers that serve that
   category as a search allowlist. A hard cap on batches per run bounds the cost; leftovers roll to the next run.
2. **Search** - one Claude request per batch with the server-side web search tool restricted to those domains, a
   search budget (`max_uses`), a cached static system prompt, and a strict JSON output schema. The model searches in
   Dutch, reads the results and returns offers (price, pack size, promo, validity, URL, evidence, confidence).
3. **Store** - offers become `observations`; anything from a domain outside the allowlist or from a blocked shop is
   discarded. Per-unit prices are computed from pack size so 8 rolls vs 24 rolls compare fairly.
4. **Assess** - each offer is compared with the item's baseline (median of the last 90 days), the shop's own regular
   price, and your target prices. A deal needs ≥ the threshold discount (default 10%) or a target hit; low-confidence
   offers are never called deals.
5. **Report** - markdown + HTML report: deals ranked first, then "checked, no deal", not found, errors, deferred items.
   Saved in the DB and as `data/reports/run-NNNN.md`; optionally e-mailed or sent to Telegram.
6. **Repeat** - on the schedule (default Monday & Thursday 07:00) while `dealwatch serve` runs, or via `dealwatch run` from cron.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · design choices: [docs/DECISIONS.md](docs/DECISIONS.md) ·
what's next: [docs/ROADMAP.md](docs/ROADMAP.md) · running it as a service: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) (installs Python 3.12 itself) and an Anthropic API key.

```bash
git clone git@github.com:Khalajestani/dealwatch.git && cd dealwatch
uv sync
cp .env.example .env            # put ANTHROPIC_API_KEY=... in .env
uv run dealwatch serve          # http://127.0.0.1:8765
```

Try it without spending credits: `DEALWATCH_FAKE_LLM=1 uv run dealwatch serve` uses a fake search client with made-up prices.

First session: add items (or click *Add sample items*), review *Retailers*, press *Dry run* to see the plan and cost
estimate, then *Run now*. The report appears under *Runs* in a minute or two.

## Using it

**Items** - name, brand, category, *search hints* (pack size, variant, model number - Dutch works best), a *unit* for
per-unit comparison (roll, tablet, wash, ml...), whether household multi-packs are acceptable, a priority, optional
target prices (per pack and/or per unit) and an optional custom check interval. Actions: *Bought it* (snoozes the item
for 30 days), *Check in next run*, pause, delete. Each item page shows its full price history.

**Retailers** - the configuration page for shops. Each has a domain and a status:
`enabled` (searched), `disabled` (temporarily off), `blocked` (never used; offers from it are discarded even if they
slip through). Category tags restrict a shop to the batches where it makes sense (Gamma for tools, Kruidvat for
personal care, empty = everything). *Search mode* in Settings switches between an allowlist of enabled shops
(default, fewer wasted searches) and searching the open web minus blocked shops.

**Runs** - *Dry run* builds the plan and the cost estimate without calling the API. *Run now* runs in the background.
Each run page shows the report, every offer seen, the search queries the model actually ran, discarded offers,
errors, and token/search usage with the estimated cost.

**Settings** - model and effort, search mode, batch size and caps, deal threshold, baseline window, check intervals,
locale, schedule (cron), notifications. Everything has inline help.

## Cost model

| Component | Price | Typical run (25 items, 5 batches) |
|---|---|---|
| Web search | $10 per 1,000 searches | 25-35 searches → $0.25-0.35 |
| Claude Opus 5 tokens | $5 / $25 per 1M in/out | ~60-90k in, ~5k out → $0.40-0.60 |
| Claude Sonnet 5 tokens (alternative) | $2 / $10 per 1M | → $0.15-0.25 |

Levers, all in Settings: batch size (fewer calls), max batches per run and max searches per batch (hard caps), effort
(`medium` is the default), model, and per-priority check intervals (wish-list items every two weeks instead of twice a
week). The planner's estimate is rough; the real usage is stored on every run.

## CLI

```bash
uv run dealwatch serve                 # web UI + scheduler
uv run dealwatch run [--force] [--dry-run] [--fake]   # one run now (for cron); prints the report
uv run dealwatch plan                  # what the next run would do and cost
uv run dealwatch report [RUN_ID]       # print a report
uv run dealwatch items list|add|remove|bought
uv run dealwatch retailers list|add|status DOMAIN enabled|disabled|blocked
uv run dealwatch settings show|set KEY VALUE
uv run pytest                          # tests never touch the API
```

## Project layout

```
src/dealwatch/
  cli.py            Typer commands
  config.py         env vars, model price table
  settings.py       user settings (DB-backed) + validation + settings-page specs
  models.py         SQLAlchemy models: Item, Retailer, Run, RunItem, Observation, Report, Setting
  db.py             engine/session, additive column migration
  seed.py           default Dutch retailers, sample items
  domains.py        domain normalisation/matching
  services/
    planner.py      due logic, grouping, budgets, cost estimate
    prompts.py      system prompt, JSON output schema, pydantic validation
    llm.py          Anthropic client (web search + structured output, pause_turn, usage) and the fake client
    pricing.py      unit prices, baselines, deal assessment
    runner.py       run orchestration (parallel batches, persistence, status)
    report.py       markdown/HTML report
    notify.py       e-mail / Telegram
    scheduler.py    APScheduler cron job
  web/              FastAPI app, Jinja templates, CSS
tests/              pytest suite (fake/stubbed LLM only)
docs/               architecture, decisions, roadmap, operations
deploy/             launchd example
data/               (git-ignored) SQLite DB + report files
```
