# Connecting Claude to your Claude plan

Three things in Dealwatch use Claude: the **price checks** (Claude searches the shops you allow and records what
it sees), the **smart entry** on Add item (words, voice, a photo or a product link become a filled-in review card)
and **Ask**. Without a connection the app says "Claude not connected" and everything else works: the list, the
forms, the retailers, the price history.

Claude runs on your Claude subscription (Pro or higher), through Claude Code, the same way as in ENDUR∆NCE and
TOOL·E. There is no API key and no separate bill: its use counts against the same plan limits as your own use of
Claude. When a limit is reached it stops and says when it resets. Why: [decisions.md](decisions.md) D17.

## 1. Make sure nothing can be charged

On claude.ai open Settings, Usage, and check that **extra usage** is switched **off**.

The app adds its own guards: it removes any API key or cloud credential from Claude Code's environment, and it
stops the moment Claude reports the plan limit as reached. A price check then leaves the remaining items due for
the next day.

## 2. Get a token (on the Mac)

In this folder:

```bash
make assistant-token
```

Sign in with your Claude account in the browser window and approve. The terminal prints a token starting with
`sk-ant-oat`, valid for one year. Use a new one for Dealwatch rather than another app's, so each can be cut off
alone; all of them draw on the same plan allowance. Treat it like a password.

## 3. Put it on the Pi

```bash
ssh -t pi-apps /srv/dealwatch/make-env.sh --assistant-token
```

Paste the token when asked (nothing shows) and press Enter. It is stored in `/srv/dealwatch/.env`, readable only
by you, and the app restarts.

For the Mac (`make dev`), put `DEALWATCH_CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...` in this folder's `.env` instead.

## 4. Check it works

1. The red "Claude not connected" badge is gone.
2. Ask: "What is on my list?" The answer names your items.
3. Add item, Say it or snap it: "Page toilet paper 24 rolls, buy under 30 cents a roll". The review card shows
   the name, brand, unit roll and the target 0.30 per unit.
4. Checks, Check prices now. After a few minutes the check shows offers and what Claude searched and read.

## 5. What it sends and what it may do

| | Model | Tools | Web |
|---|---|---|---|
| Price check, one run per batch of about 6 items | Settings (Sonnet by default) | `record_item`, one call per item | Searches and pages of the batch's shops only, at most the batch's budget (Settings, 10 by default) |
| Smart entry | Sonnet, low effort | none | Only the shop of a link in your words, at most 3 lookups |
| Ask | Picker on the page, Haiku by default | read an item, latest deals, add, change, bought, already bought, undo, pause, resume, check next | none |

The web limits are enforced by the app before each search or page read (a hook in `services/claude.py`), not left
to the model: a search is always pinned to the allowed shops, a page from another site is refused, and nothing
runs once the budget is used. Offers from other sites are dropped again when stored.

Ask sends the instructions (`src/dealwatch/services/ask.md`), today's date, one compact line per item (name,
priority, targets, status, the best offer of its latest check) and the last 20 turns. A photo added on Ask or in
the smart entry is shrunk to 1568 px and sent with the words; on Ask it is not kept, in the smart entry it is kept
with the item only when you press Add item. Speech is turned into text in the browser; only text is sent.

Nothing can be deleted through Claude. Deleting items and shops is done in the app.

## 6. If it does not work

| The app says | Meaning | Fix |
|---|---|---|
| "Claude not connected" | No token | Step 3 |
| "could not sign in to your Claude plan" | The token is wrong, revoked, or a year old | Steps 2 and 3 |
| "usage limit is reached until 15:00" | The plan's allowance is used up for now | Wait; the next daily check picks up what was left |
| "could not answer just now" | Claude Code failed or Claude was unreachable | Try again; the reason is in `ssh pi-apps 'cd /srv/dealwatch && docker compose logs --tail 50'` |
| "took too long and was stopped" | No answer in time (3 minutes, 10 for a batch) | Try again; for checks, fewer items per batch in Settings |
| "Could not tell which product this is" | Nothing usable in the words or the photo | A few more words, or the photo closer with the label in view |

## 7. Renewing and revoking

The token lasts one year; then do steps 2 and 3 again. To cut it off earlier, revoke it at claude.ai under
Settings, Claude Code, then check that Ask answers "could not sign in".
