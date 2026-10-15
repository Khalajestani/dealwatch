# Dealwatch

A small private web app that keeps your household shopping list and wish list and, every day, has Claude check the
items that are due at the Dutch shops you allow. It remembers every price it sees, works out what "normal" is per
item, and reports only the offers that are genuinely below normal, below your target, or a real promotion, with
links, evidence and a confidence score.

You decide what to buy. Dealwatch never orders anything.

It runs on the Raspberry Pi at `https://dealwatch.home.arpa` (home network and VPN only) and works as a Home Screen
app on the iPhone. Claude runs on your Claude plan: no API key, nothing billed per check ([docs/assistant.md](docs/assistant.md)).

## Honest assessment

- **It is a research assistant, not a price-comparison engine.** Search results can be stale or ambiguous (pack
  sizes, marketplace sellers, "vanaf" prices). Every price carries the source link, a short quote and a confidence
  score; treat the report as a shortlist.
- **The value builds over time.** At first there is no history, so only shop-declared promotions and target prices
  count. After a few checks the per-item baseline (median of the best price per check) makes "is this actually
  cheap?" reliable. Give it 4 to 6 weeks.
- **Where it pays off:** stocking up on consumables in 1+1 and 2e-halve-prijs weeks (per-unit prices make bulk packs
  comparable), and waiting for a target price on wish-list items. Where it does not: items whose price barely moves.
- **It uses your plan's allowance.** A batch of 6 items is one Claude run of one to three minutes with a handful of
  searches. The daily check only takes the items that are due, so a typical day is one or two batches. If the plan's
  limit is reached, the check stops and the rest waits for the next day.
- **Supermarket offers are structured data**: reading them directly is the top item on the [roadmap](docs/roadmap.md).

## How it works

1. **Plan**: the active items that are due (need every 3 days, stock up weekly, wishlist every 2 weeks) are ordered
   by urgency, grouped by category and split into batches of about 6, each with the enabled shops for that category.
2. **Search**: one Claude run per batch. The app holds every search and page read to those shops and to a budget
   of lookups; Claude records each item's offers (price, pack, promotion, validity, URL, evidence, confidence).
3. **Store and assess**: offers from other sites are dropped, per-unit prices computed, and each offer compared with
   the item's baseline, the shop's regular price and your targets. A deal needs at least the threshold discount
   (default 10%) or a target hit; low-confidence offers are never called deals.
4. **Track**: one price point per item per check, the series behind the Tracker, the charts and the exports.
5. **Report**: deals first, a price tracker section (cheaper or dearer than last time, new lows), then checked
   without a deal, not found and errors. The dashboard shows the latest deals.

Details: [architecture](docs/architecture.md) · [decisions](docs/decisions.md) ·
[requirements](docs/requirements.md) · [on the Pi](docs/raspberry-pi.md) · [Claude](docs/assistant.md) · [recovery](docs/recovery.md)

## Using it

**Add item**: "Say it or snap it": type or tap the mic and say what to watch ("Page toilet paper 24 rolls, buy
under 30 cents a roll"), add a photo of the pack, or paste a product link. Claude fills in a review card: name,
brand, category, priority, pack and unit, Dutch search hints, any target you named. Nothing is saved until you
press Add item, and likely duplicates are pointed out. "Fill in the form" does the same by hand; only the name is
required.

**Ask**: by text or voice, optionally with a photo. "What are the best deals right now?", "Is the coffee cheaper than
usual?", "I bought toilet paper today", "Watch Dreft dishwasher tablets, I need them soon". It can read an item's
history, add, change, and mark items bought, paused, resumed or due for the next check. It cannot delete.

**Items**: filters by text, category, priority and status (due, waiting, snoozed, paused, has a deal, no price yet).
Each item page has its price tracker, chart, per-shop breakdown and every offer seen. Actions: Bought it (snoozes for
30 days), Check in the next price check, Pause, Delete.

**Retailers**: each shop is enabled (searched), disabled (temporarily off) or blocked (never used; offers from it
are dropped). Category tags limit a shop to the batches where it makes sense. Settings, Search mode switches between
enabled shops only (default) and the open web minus blocked shops.

**Checks**: the daily check at 07:00, or Check prices now. Each check shows the report, every offer, what Claude
searched and read, dropped offers and errors.

**Settings**: model and effort, search mode, batch size and caps, deal threshold, baseline window, check intervals,
locale, schedule. Everything has inline help.

## Your data

Everything is in one SQLite file, `data/dealwatch.db`, backed up every night with the item photos (newest 30 kept, `make pi-backups`
copies them to the Mac, `make pi-restore` puts one back; [docs/recovery.md](docs/recovery.md) when data is lost or wrong). CSV exports are linked in the footer: `/export/price_points.csv`, `observations`,
`items`, `runs`, and `/items/{id}/price_points.csv`.

| Table | One row per | Notes |
|---|---|---|
| `observations` | offer seen | price, regular price, pack, unit, unit price, promotion, confidence, evidence, URL, the deal assessment |
| `price_points` | item and check | min, average, median and max per pack and per unit, the best offer, promotion and deal counts |
| `run_items` | item and check | found, not found or error, with Claude's note |
| `runs` | price check | status, batches, web lookups, plan and log as JSON |

## On the Mac

Requirements: [uv](https://docs.astral.sh/uv/) (installs Python 3.12 itself).

```bash
uv sync
cp .env.example .env          # safe as it is; add DEALWATCH_CLAUDE_CODE_OAUTH_TOKEN to use Claude
make dev                      # http://127.0.0.1:8765
make test                     # never reaches Claude
make lint
```

Deploying and day-to-day running on the Pi: [docs/raspberry-pi.md](docs/raspberry-pi.md).

## Project layout

```
src/dealwatch/
  config.py         process settings from DEALWATCH_* environment variables
  settings.py       settings edited in the app (database), validation, settings-page specs
  models.py         Item, Retailer, Run, RunItem, Observation, PricePoint, Report, Setting, ChatMessage
  db.py             engine, session, additive columns at start
  items.py          the one write path for items (forms and Ask)
  queries.py        reads shared by the pages and Ask
  security.py       login, same-origin check, security headers
  backup.py         nightly backup (also `dealwatch-backup`)
  seed.py, domains.py
  services/
    claude.py       the one place that runs Claude Code: plan only, web policy, errors as sentences
    tool.py         typed functions as tools
    planner.py      due logic, batches, budgets
    search.py       one Claude run per batch, the record_item tool, the instructions
    runner.py       a price check from plan to report
    pricing.py      unit prices, baselines, deal assessment, price points
    insights.py     per-item statistics, per-shop breakdown, overview
    report.py       markdown report
    extract.py      smart entry: words, photo or link to a draft item
    ask.py, ask.md  the Ask page's turn, snapshot and tools
    similar.py      duplicate detection
    scheduler.py    the daily check and the nightly backup
  web/              app.py (routes), templates, static (style.css, app.js, tracker.js, PWA files), charts.py
tests/              pytest, with stand-ins for Claude (conftest.py)
deploy/             dealwatch/ (compose, make-env.sh), proxy/ (site file), ship.sh
docs/               requirements, decisions, architecture, assistant, raspberry-pi, recovery, roadmap
```
