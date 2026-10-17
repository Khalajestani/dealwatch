# Requirements

What was asked, in the owner's words where there are some, and where it lives. Newest at the bottom.

## R1. A deal watcher for the household list (09/2026)

Keep a shopping and wish list, have Claude find current prices and promotions at the Dutch shops the owner allows,
remember every price, and report only real deals against each item's own history. Three ways to add an item with a
review card, a price tracker with charts, CSV exports. See the README.

## R2. On the Raspberry Pi with Claude on the plan, like ENDUR∆NCE and TOOL·E (14/10/2026)

> "Similar to what we implemented on Endurance app repo which then followed by applying the same changes to the
> TOOL-E app repo, I now want to implement the applicable change to the dealwatch app. I want it to follow the
> deployment process of the other two apps, live and run on my raspberry pi and have daily scheduled jobs running
> and have the updates etc there. Also want to integrate the similar "ask" feature and smart entries with photo and
> voice / text to LLMs for entering new items to search for and other functionalities of the app. [...] Similarly I
> don't want a logout button either there. Do a review and sanity check on the shopping assistance and deal finder
> app. implement changes, made the code lean and clean, drop not needed, redundant and or obsolete parts as well.
> deploy the changes to the pi, access firewalla for changes required."

Followed ENDUR∆NCE's `docs/19-playbook-for-new-apps.md` and TOOL·E's latest state.

| Asked | Where |
|---|---|
| Runs on the Pi at `https://dealwatch.home.arpa`, home network and VPN only | `Dockerfile`, `deploy/dealwatch/compose.yaml`, `deploy/proxy/dealwatch.caddy`, [raspberry-pi.md](raspberry-pi.md) |
| Built on the Mac, shipped over SSH | `deploy/ship.sh`, `make pi-ship` |
| Secrets typed by the owner on the Pi only | `deploy/dealwatch/make-env.sh` |
| Data can be put back after a fix or an incident, by an agent too | `backup.py` (`restore`), `make pi-restore`, [recovery.md](recovery.md), D26 |
| Daily scheduled jobs: the price check and the backup | `services/scheduler.py`, daily at 02:30 (D15, D28), backup at 03:00 |
| The updates there: deals on the dashboard, a report per check, the tracker | Dashboard, Checks, Tracker pages; a Home Screen app (`manifest.webmanifest`, `sw.js`) |
| Claude on the owner's plan, no API key, nothing billed | `services/claude.py`, [assistant.md](assistant.md), D17 to D19 |
| Ask, by text or voice, with a photo | `/ask`, `services/ask.py`, `ask.md`, D22 |
| Smart entry with photo, voice or text | Add item, "Say it or snap it", `services/extract.py`, D21 |
| No logout button | `security.py`, D20 |
| Review and sanity check; lean and clean | D16, D18, D23; one write path for items (`items.py`), shared reads (`queries.py`) |
| The Firewalla DNS rule | `dealwatch.home.arpa` to 192.168.54.46, raspberry-pi.md step 6 |

Owner rules carried over: no payment outside the Claude subscription; private; clean and lean; the owner types
every secret; dates shown dd/mm/yyyy; no em-dashes in UI text or docs.

## R3. A clearer, mobile-friendly UI (16/10/2026)

> "I do not like the current main page and the overall UI of this APP. It has the useful functionalities I want but
> I want an better presentation of the data. On main page I want a subtle indication last run did run and was
> successful, then any alerts for new lower process found if it did, a place I can lookup what have been being
> checked. also the add item should have a edit part that looks similar to the add part, but remove the clutter and
> make it easier to use and add / modify things. keep the data points only useful for the actual functionality that
> is searching for new low prices and deals. All these elements are there but I want a better and mobile friendly
> UI. Take any decision base on the Design recommendations and best practices. add some subtle style and colours too."

| Asked | Where |
|---|---|
| A subtle sign the last check ran and went well | Home, the status line (`dashboard.html`) |
| Alerts for new lower prices | Home, Deals (`queries.current_deals`) and Price drops (`insights.price_drops`) |
| A place to look up what is being checked | Home, Watching (`planner.due_at`); Items with status chips; Checks |
| Edit that looks like Add, less clutter, only what the search uses | `item_form` in `_macros.html`, `item_new.html`, `item_edit.html`, D27 |
| Mobile friendly, subtle style and colour | `style.css`, the tab bar in `base.html`, D27 |

## R4. The check before the morning; an Already bought button (17/10/2026)

> "Move the search to 02:30 Europe/Amsterdam [...] Starting the run at 02:30 means its window closes by 07:30, so I
> get a fresh window when I start using Claude in the morning." And: "one click marks an item as purchased. It
> stays in the history, but the app stops searching for it and stops reporting it." Asked about the existing Bought
> it: refills like toilet paper skip two months after a big buy, things like a charger are archived for good.

| Asked | Where |
|---|---|
| 02:30 Europe/Amsterdam, daily, across summer and winter time | `schedule_time` in `settings.py`, `scheduler.check_trigger`, D28 |
| The start time as one setting | Settings, Schedule, Start time |
| Nothing else sends Claude requests between 07:30 and the next check | Only the check is scheduled to reach Claude (`scheduler.py`), D28 |
| Start and end of each check in the log | `runner._execute`, `runner._log_end` |
| Status active or bought with bought_at, existing items active | `Item.status`, `Item.bought_at`, added by `db.init_db` |
| One click, no confirmation, wherever items are listed | `bought_button` in `_macros.html` on Home and Items; both on the item page |
| Bought items out of the checks, deals and reports | `Item.watched`, `queries.current_deals`, `insights.all_item_insights`, `report.build_report` |
| Never deleted; Purchased folded away with the date and Undo | `items.html`, `undo_bought` in `items.apply_action` |
| Mark as bought in mail digests | There are none (D23) |
| Refills skip two months | Bought it, `bought_snooze_days` 60 |
