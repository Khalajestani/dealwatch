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
| Daily scheduled jobs: the price check and the backup | `services/scheduler.py`, default `0 7 * * *` (D15), backup at 03:00 |
| The updates there: deals on the dashboard, a report per check, the tracker | Dashboard, Checks, Tracker pages; a Home Screen app (`manifest.webmanifest`, `sw.js`) |
| Claude on the owner's plan, no API key, nothing billed | `services/claude.py`, [assistant.md](assistant.md), D17 to D19 |
| Ask, by text or voice, with a photo | `/ask`, `services/ask.py`, `ask.md`, D22 |
| Smart entry with photo, voice or text | Add item, "Say it or snap it", `services/extract.py`, D21 |
| No logout button | `security.py`, D20 |
| Review and sanity check; lean and clean | D16, D18, D23; one write path for items (`items.py`), shared reads (`queries.py`) |
| The Firewalla DNS rule | `dealwatch.home.arpa` to 192.168.54.46, raspberry-pi.md step 6 |

Owner rules carried over: no payment outside the Claude subscription; private; clean and lean; the owner types
every secret; dates shown dd/mm/yyyy; no em-dashes in UI text or docs.
