# Dealwatch: context for agents

Personal deal watcher for one household (NL). Items on a shopping and wish list are checked daily, when due, by
Claude (Claude Code on the owner's plan, web search held to allowed shops); every offer is stored and real deals are
reported against each item's own price history. Runs on the Raspberry Pi beside ENDUR∆NCE and TOOL·E. Read
`README.md`, then `docs/architecture.md`; `docs/decisions.md` says why, `docs/requirements.md` what was asked.

## Owner rules

- **No payment outside the Claude subscription.** No API key, no paid service. Claude only through
  `services/claude.py` (plan token, paid credentials stripped, `tools` limited, limit stops the run).
- **Private**: home network and VPN only. **Clean and lean**: one way to do each thing; remove what a change makes
  obsolete, code and docs, in the same change. **The owner types every secret.**
- No em-dashes in UI text or docs. Dates shown dd/mm/yyyy, stored ISO/UTC. Metric units.

## Stack and commands

- Python 3.12, uv, FastAPI + Jinja2 (server-rendered, no JS build), SQLAlchemy 2 + SQLite, APScheduler 3,
  claude-agent-sdk, pydantic-settings, pytest, ruff.
- `make dev` · `make test` · `make lint` · `make format` · `make pi-ship` · `make pi-status`
- Data lives in `./data` (git-ignored). Deleting `data/dealwatch.db` resets everything; retailers re-seed on start.

## Conventions

- **Never reach Claude from tests.** Use `FakeSearchClient`, `FakeClaude` or the `claude_code` fixture
  (`tests/conftest.py`).
- **Claude Code options** live in `services/claude.py` only. Keep `setting_sources=[]`, `permission_mode="dontAsk"`,
  `strict_mcp_config=True`, `tools=[]` unless a `WebPolicy` adds WebSearch and WebFetch, and the paid-credential strip.
- **Web access** is decided by `WebPolicy.decide` (the PreToolUse hook), not by the prompt. The runner drops offers
  outside the batch's shops again when storing.
- **Tools** are typed functions with an `Args:` docstring (`services/tool.py`). They return short strings, raise
  `ValueError` for the model to correct, and never delete.
- **Items are written only through `items.py`** (forms and Ask). Reads shared by pages and Ask go in `queries.py`.
- **All datetimes are naive UTC** in the DB; convert for display only (`report.local_time`, `localtime` filter).
- **Settings**: a field with a default on `settings.Settings` and a `SettingSpec` in `SPECS`; the page and
  validation pick it up.
- **Schema changes**: add the column to the model; `db.init_db()` adds missing nullable columns. Anything more needs
  a real migration step.
- **Model and web text is untrusted**: escape it in reports (`report.safe`, `report.link`); templates autoescape.
- **No inline scripts or handlers** (the CSP allows scripts from the app only): use `data-confirm`, `data-busy`
  and `static/app.js`.
- Keep docs in sync in the same change: README (user-facing), architecture (how), decisions (why), requirements
  (what was asked), assistant and raspberry-pi (operations), roadmap (next).

## Where things happen

| Question | Look in |
|---|---|
| Which items are checked and how they are batched | `services/planner.py` (`is_due`, `build_plan`) |
| The Claude run for a batch, its instructions and `record_item` | `services/search.py` |
| Plan-only run, web policy hook, errors as sentences | `services/claude.py` |
| What counts as a deal | `services/pricing.py` (`assess_offer`, `baseline_for_item`) |
| A check from plan to report, domain filter, stopping on the limit | `services/runner.py` |
| Smart entry | `services/extract.py`, `templates/item_new.html`, `item_review.html` |
| Ask | `services/ask.py`, `services/ask.md`, `templates/ask.html` |
| Schedule and backups | `services/scheduler.py`, `backup.py` |
| Recovering or reloading data (agents follow it) | `docs/recovery.md`, `backup.restore`, `make pi-backups`, `make pi-restore` |
| Login | `security.py` |
| Charts | `web/charts.py` (SVG), `static/tracker.js`, `.viz` tokens in `style.css` |
| Deploy | `Dockerfile`, `deploy/`, `docs/raspberry-pi.md` |

## Gotchas

- Only one check per process (`runner._run_lock`); batches run one after another (memory on the Pi).
- `CannotRun` (plan limit or failed sign-in) stops the rest of a check; other `Unavailable` errors fail one batch.
- Whatever `record_item` stored is kept when a batch is cut short; only unrecorded items become errors.
- The planner's `DUE_SLACK` (12h) makes items due slightly early so a fixed daily schedule does not skip a day.
- Price points have `observed_at = run.started_at`; baselines use points strictly before the check. Tests pass
  explicit `now=` values to simulate weeks passing.
- Claude Code's models are named `haiku`, `sonnet`, `opus`; the Settings choices and the Ask picker use these.
- The container's clock is UTC; anything shown or named by date uses the time zone from Settings.
- Uploaded photos live in `data/uploads/<uuid>.jpg` (shrunk JPEG); `image_path` is checked against that pattern.
