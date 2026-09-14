# Dealwatch - context for AI agents and future me

Personal deal watcher for one household (NL). Users add shopping/wish-list items; a few times a week the app asks
Claude (server-side web search, domain-restricted) for current prices/promos per batch of items, stores every
observation, and reports real deals versus each item's own price history. Read `README.md` first, then
`docs/ARCHITECTURE.md`. `docs/ROADMAP.md` lists what to build next; `docs/DECISIONS.md` explains why things are the way they are.

## Stack and commands

- Python 3.12, uv, FastAPI + Jinja2 (server-rendered, no JS build), SQLAlchemy 2 + SQLite, APScheduler 3, Typer, `anthropic` SDK 1.x.
- `uv sync` · `uv run pytest` · `uv run dealwatch serve` · `uv run dealwatch run --fake` · `uv run dealwatch plan`
- Try the UI without credits: `DEALWATCH_FAKE_LLM=1 uv run dealwatch serve`.
- Data lives in `./data` (git-ignored). Deleting `data/dealwatch.db` resets everything; retailers re-seed on start.

## Conventions

- **Never call the real API from tests.** Use `FakeSearchClient` or a stub client object (see `tests/test_llm.py`).
  Anything that spends credits must be an explicit user action (Run now / `dealwatch run`).
- **All datetimes are naive UTC** in the DB; convert only for display (`report.local_time`, `localtime` filter).
- **Settings**: add a field with a default on `settings.Settings` and a `SettingSpec` in `settings.SPECS`. The settings
  page, validation and CLI pick it up automatically.
- **Schema changes**: add the column to the model; `db.init_db()` adds missing nullable columns with `ALTER TABLE`.
  Anything more (renames, NOT NULL) needs a real migration step - keep it simple, this is a personal tool.
- **Prompt caching**: `prompts.SYSTEM_PROMPT` must stay static (no dates, no per-run data). Per-run data goes in the
  user message (`build_user_message`).
- **Model-supplied text is untrusted**: escape it in reports (`report.safe`, `report.link`) and templates (autoescape on).
- **Structured output schema** (`prompts.OUTPUT_SCHEMA`): every object needs `additionalProperties: false` and all
  properties listed in `required`; nullable = `anyOf [type, null]`; no `minimum`/`maximum`/`pattern`.
- Web search tool: `allowed_domains` and `blocked_domains` are mutually exclusive - the planner picks one based on
  `search_mode`. Domains must be bare ASCII (`domains.normalize_domain` / `is_valid_domain`).
- Keep docs in sync: README (user-facing), ARCHITECTURE (how), DECISIONS (why), ROADMAP (next). Update them in the same change.

## Where things happen

| Question | Look in |
|---|---|
| Which items are checked and how they are batched | `services/planner.py` (`is_due`, `build_plan`) |
| The exact API request (tools, schema, effort, caching) | `services/llm.py` (`build_request`) and `services/prompts.py` |
| pause_turn continuation, usage accounting, error mapping | `services/llm.py` (`AnthropicSearchClient.search_group`) |
| What counts as a deal | `services/pricing.py` (`assess_offer`, `baseline_for_item`) |
| Run lifecycle, status, per-batch persistence, domain post-filter | `services/runner.py` |
| Report text | `services/report.py` |
| Cron schedule | `services/scheduler.py`, setting `schedule_cron` |
| Cost estimate and price table | `services/planner.py` (`estimate_plan`), `config.py` (`MODEL_PRICES`) |
| Price points (the tracked series) | `services/runner.py` (`_record_price_points`), `services/pricing.py` (`summarize_offers`) |
| Tracker stats, movers, per-shop breakdown | `services/insights.py` |
| Direct deal sources (AH/Jumbo/Lidl adapters go here) | `services/sources/__init__.py` (`DealSource`, `SOURCES`, `FakeSource`), `runner._run_sources` |
| Charts | `web/charts.py` (SVG), `web/static/tracker.js` (hover), `.viz` tokens in `style.css` |
| CSV / JSON exports | `web/app.py` (`/export/{table}.csv`, `/api/items/{id}/prices`), `cli.py export` |
| Web routes | `web/app.py` (single file, grouped by page) |

## Gotchas

- `run.cost_usd` is an estimate from `config.MODEL_PRICES` - update the table when Anthropic prices change.
- Only one run per process (`runner._run_lock`). The scheduler and the UI both go through `execute_run`.
- With `search_trim_response` on, the API omits raw search result blocks from responses (cheaper); the model's queries
  are still logged when present. Turn it off to debug what the model saw.
- `claude-haiku-4-5` (and other pre-4.6 models) falls back to the basic `web_search_20250305` tool without effort/thinking
  params (`llm.model_profile`).
- Structured outputs plus server tools: the JSON is the final text block; `llm.final_text` picks the last text block
  containing `{` and `extract_json` tolerates fences/prose.
- The planner's `DUE_SLACK` (12h) makes weekly items due slightly early so a fixed weekly schedule doesn't skip them.
- Price points have `observed_at = run.started_at`; baselines use points strictly before the run, so a run never
  compares against itself. Tests pass explicit `now=` values to simulate weeks passing.
- A new source adapter must only return offers from its own domain (others are dropped) and runs only when that
  retailer is `enabled` on the Retailers page. Keep adapters free of network calls at import time; test with fixtures.
- Charts: keep to the two series colours (`--viz-s1/--viz-s2`) - adding a third series needs a re-validated palette.
