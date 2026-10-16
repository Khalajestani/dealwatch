# Decisions log

Short entries: what was decided, why, and what it would take to change. Newest at the bottom. An entry that a later
one replaced says so in its first line.

## D1. Python, FastAPI and SQLite, server-rendered UI (14/09/2026)

One process, one database file, no JavaScript toolchain: easiest for a personal tool to run for years and for an
agent to change. uv pins Python 3.12.

## D2. Claude searches the shops instead of scraping them (14/09/2026)

Retailer sites (bol, AH, Kruidvat) have bot protection and change their layout; scraping is fragile and against most
terms of use. Claude's web search returns current results with URLs, and an allowlist of domains maps one to one
onto the Retailers page. Trade-off: prices come from search results and pages the model reads, so every offer
carries evidence and a confidence, and only the price history makes them trustworthy. Structured shop sources are
on the roadmap. Since D17 the search is Claude Code's own, held to the shops by the app (D19).

## D3. One Claude run per batch of about 6 items, grouped by category (14/09/2026)

Grouping shares the instructions and lets one shop list fit every item in the run. Larger batches read worse;
4 to 8 is the sweet spot.

## D4. Deal means cheaper than its own history first (14/09/2026)

Shop "van/voor" prices are marketing; the median of what we have seen over 90 days is the honest baseline. A first
offer can only be judged by the shop's regular price, a target price or a clear promotion, and the report says so.

## D5. Prices per unit (14/09/2026)

Household bulk packs are the point of stocking up; comparing per roll, per wash or per 100 ml makes an 8-pack and a
24-pack comparable.

## D6. Allowlist search by default (14/09/2026)

Searching only enabled shops wastes fewer lookups and keeps results usable. Open mode exists for discovery, with
blocked shops excluded.

## D7. Naive UTC times, additive migrations (14/09/2026)

Times are stored in UTC and shown in the time zone from Settings, so the container's UTC clock never shifts what is
shown. New tables and nullable columns are added at start (`db.init_db`); anything more needs a real migration.
The "no login" part of this entry is replaced by D20.

## D8. Price points are the tracked series; the baseline is the median of the best price per check (14/09/2026)

Raw offers mix cheap and expensive shops, so their median made mediocre prices look like deals. One row per item
per check (min, average, median, max, per unit, best offer) is what a person means by "the price that week". Raw
offers stay for checking and per-shop statistics. Since 14/10/2026 the baseline reads price points only (the
fallback to raw offers was for data from before price points, and there is none).

## D9. Charts are server-rendered SVG with a small hover script (14/09/2026)

No chart library: the app stays dependency-free and works offline. Two validated series colours, sparse labels, a
legend and a table under each chart.

## D10. A review card before every add; Claude helps, the owner decides (14/09/2026)

The smart entry fills in the form, the owner confirms. Claude's summary and confidence are shown so a bad read is
obvious. Only the name is required anywhere.

## D11. Duplicate detection is lexical, not a Claude call (14/09/2026)

Token overlap with a small Dutch to English word list plus a typo ratio is instant, free and predictable. It runs
while typing, on the review card and when Ask adds an item.

## D12. UI: server-rendered, inline SVG icons, restrained colour (14/09/2026)

White cards on a light page, one indigo accent for actions, colour only for states and categories. Dark mode
follows the device.

## D13. Fakes for tests, never the real Claude (14/09/2026)

Tests use a made-up price source (`tests/conftest.py`), a stand-in for the Claude wrapper and a scripted stand-in
for Claude Code itself. Since 14/10/2026 the fakes live in the tests only: there is no fake mode in the app.

## D14. Hosted on the Pi beside ENDUR∆NCE and TOOL·E, through the shared proxy (14/10/2026)

The Pi, the Caddy proxy, the private certificate authority and the device trust were already there. Dealwatch adds
its folder `/srv/dealwatch`, its container on the `proxy` network with no published port, one site file and one DNS
rule. As in TOOL·E (its D2), the site file lives here (`deploy/proxy/dealwatch.caddy`) and `deploy/ship.sh site`
writes it to the Pi, so ENDUR∆NCE's repository is not touched. The launchd file for the Mac is gone: one way to run it.

To change: another host needs Docker, the `proxy` network and the same site file.

## D15. A daily check; priorities still decide how often an item is looked at (14/10/2026)

The owner asked for daily scheduled jobs. The default schedule is every day at 07:00 instead of Monday and Thursday.
Each run only takes the items that are due by their priority's interval, so a daily run does not mean every item
every day: it means an item never waits more than a day past its interval. The nightly backup at 03:00 is the second
job; both live in the one scheduler inside the web process.

To change: Settings, Schedule (cron), and the check intervals per priority.

## D16. Checks run one batch after another (14/10/2026)

Each batch is a Claude Code process of about 300 MB. Running them in parallel on the Pi would double the memory for
a check that runs at 07:00 nobody waits for. The parallel-batches setting is gone.

## D17. Claude runs on the owner's plan through Claude Code; no API key (14/10/2026)

Replaces the Anthropic API client (`ANTHROPIC_API_KEY`, billed per token and per search), its cost table, cost
estimates, the dry run that showed them, prompt caching and server-side fallbacks. The owner's rule is no payment
outside the Claude subscription. Same design as ENDUR∆NCE D39 to D41 and TOOL·E D3: each call is one run of Claude
Code started by the Agent SDK, signed in with a plan token, with no settings, memory or session read or kept. The
five credentials that would be billed are removed from the environment before it starts, and a reached plan limit
or a failed sign-in stops the run and the rest of that check (items stay due). One file, `services/claude.py`,
does this for the checks, the smart entry and Ask.

Models are Claude Code's names on the plan: Sonnet by default for checks (reading prices off pages is worth it),
Opus or Haiku in Settings. A run records its web lookups instead of tokens and dollars.

To change: if plan logins for the Agent SDK are ever closed, Claude stops and nothing is charged; the app keeps
working without it.

## D18. Results come back through a tool, not as one JSON reply (14/10/2026)

Claude Code's structured output works through a built-in tool that the lockdown would refuse, and one large JSON
reply is lost entirely when a run is cut short. Instead Claude calls `record_item` once per item. Its arguments are
checked like a form (a wrong field goes back to the model as an error to fix), and whatever was recorded is kept
when the run times out or hits the limit; only the items it did not get to stay due. The smart entry, which answers
with one small object, uses JSON in plain text like TOOL·E's photo recognition.

## D19. The web is held to the shops by the app, not by the prompt (14/10/2026)

Claude Code gets exactly two of its own tools for checks, WebSearch and WebFetch, and none for Ask. A PreToolUse
hook sees every search and page read before it runs: it pins each search to the batch's allowed shops (or away from
the blocked ones in open mode, whatever the model asked), refuses pages from other sites, and refuses everything once
the batch's budget of lookups is used. The budget replaces the API's `max_uses`. Offers from other domains are
dropped again when stored. Ask has no web on purpose: prices come from the checks, which keeps answers grounded in
the recorded history and quick.

## D20. One password login, no sign-out (14/10/2026)

Same as TOOL·E D6 and R7: one password, a signed cookie for 90 days per device, HTTPS-only on the Pi, eight wrong
passwords lock the login for ten minutes, redirects stay on the site, state-changing requests must come from the
app's own pages. The app refuses to start with a password and the built-in or a short signing key. There is no
sign-out button; a new password (`make-env.sh --replace`) signs every device out. `/healthz` stays public for the
container health check.

## D21. Smart entry: one box for words, voice, a photo and a link (14/10/2026)

The three tabs (type it, from a link, from a photo) became two: "Say it or snap it" and the plain form. The box
takes typed or spoken words (speech is turned into text in the browser), an optional photo and, when the words hold
a product link, Claude reads that page (and may search that shop only). Claude also picks up what was said about
buying it: running low becomes priority need, "under 30 cents a roll" becomes the unit target. Several items at once
go through Ask, which adds them directly and checks for duplicates.

## D22. Ask: questions about prices and keeping the list, by voice or text (14/10/2026)

Like TOOL·E's Ask. Haiku by default with a picker remembered on the device. It can read an item's history, list the
latest deals, add (refusing likely duplicates unless confirmed), change, and mark bought, paused, resumed or due; it
cannot delete. A photo can go with a question, for example a shelf label to compare with the history.

## D23. Removed: the command line, notifications, the deal-source scaffold, report files (14/10/2026)

- The Typer CLI repeated what the pages do; on the Pi everything is in the app. `dealwatch-backup` stays.
- E-mail and Telegram reports were never configured and would need more secrets on the Pi; the dashboard is the
  place to look, on the Home Screen. To bring one back: a step after `runner._finalize` and its secrets in
  `make-env.sh`.
- The direct deal-source interface had no source; it is described on the roadmap instead.
- Report files in `data/reports/` duplicated the database; `/runs/{id}/report.md` serves the same text.
- The JSON endpoints `/api/health`, `/api/runs/latest` and `/api/items/{id}/prices` had no user; `/healthz`
  replaces the first. CSV exports stay.

## D24. Dates as dd/mm/yyyy (14/10/2026)

Owner convention across the apps. Times show as "Wed 14/10/2026 07:00" in the time zone from Settings.

## D25. Number keypads, a comma as decimal point, readable image files (15/10/2026)

Carried over from ENDUR∆NCE. Prices ask for the decimal keypad and counts for the digit pad (`inputmode`), and the
fields stay text fields: a phone set to a Dutch region offers only a comma, which a number field would drop. The
server already read "4,50" for target prices; settings now do too. The Dockerfile makes `/app` readable for the
app user whatever the modes on the Mac (an owner-only template was a 500 on the Pi there), and a test fails on an
unreadable file under `src`.

## D26. A checked restore an agent can run (15/10/2026)

Putting a backup back meant typing file commands on the Pi, and no backup had been copied off it yet.
`dealwatch-backup --restore ARCHIVE` checks the archive (a whole SQLite database, nothing unpacking outside its
folder), backs up what is there, then swaps the database and photos; the app adds the columns an older database
lacks on start. `deploy/ship.sh restore` runs it on the Pi with the app stopped and starts it again either way.
`make pi-backups` writes a fresh archive before copying, so the Mac gets the state of that moment.
[recovery.md](recovery.md) is written for an agent: rules (copy first, never edit on the Pi, code before data) and
the usual cases. ENDUR∆NCE and TOOL·E work the same way.

## D27. A mobile-first redesign: a quiet status line, alerts, what is watched (16/10/2026)

The owner found the dashboard and the overall UI hard to read on the phone (R3). Home is now one status line for the
last check (a green dot when it went well, red with the reason when it failed, Check now beside it), then the alerts
(deals, price drops), then Watching: the active items, the next to be checked first. The four stat tiles are gone;
their numbers are in the Watching count and the status line. Lists are rows with a category tile instead of wide
tables; tables stay only on the detail pages, where they scroll sideways. On a phone a bottom tab bar (Home, Items,
Add, Ask, More) replaces the two rows of pills, and the CSV links moved from the footer to the Tracker page.

- **Deals are each item's deal from its latest check**, not only the last check's: a daily check takes only the items
  that are due, so a one-item check emptied the dashboard while other deals were still on. Ended promotions are left
  out; the last check's finds are marked new. Ask's latest_deals reads the same (`queries.current_deals`).
- **One item form for adding and editing** (`item_form`), in three short sections, asking only what the checks use:
  name, brand, category and search words; priority as a switch that shows each interval; the targets and bulk packs;
  the check interval folded away. Notes and the product link are not asked any more (the search does not read them);
  what the smart entry or Ask put there is kept and shown on the item page. Editing is its own page like Add, with
  Delete at the bottom; a wrong value shows the form again with what was typed.
- **Typing an item in adds it directly.** The live similar-items hint under the name already warns of duplicates, so
  the review card stays for the smart entry, where Claude filled the fields in (D10 still holds for that).
- Colour: the indigo accent stays (D12), a warm orange marks deals, and each category has its own tint, mixed into
  the surface so both themes read. Fields use 16 px text so the iPhone does not zoom in, and buttons are at least
  42 px high.

To change: the parts are in `style.css` and `_macros.html`; the home page's choices are in `dashboard.html` and the
`dashboard` route.
