# Operations

## Running it permanently on a Mac (launchd)

`deploy/com.dealwatch.serve.plist` keeps `dealwatch serve` running (web UI + scheduler) and restarts it on login.

```bash
mkdir -p data/logs
sed "s#__PROJECT_DIR__#$(pwd)#g; s#__UV__#$(which uv)#g" deploy/com.dealwatch.serve.plist > ~/Library/LaunchAgents/com.dealwatch.serve.plist
launchctl load ~/Library/LaunchAgents/com.dealwatch.serve.plist      # start now + at login
launchctl unload ~/Library/LaunchAgents/com.dealwatch.serve.plist    # stop
tail -f data/logs/serve.log
```

The Mac must be awake at the scheduled time (System Settings → Energy, or `caffeinate`); otherwise APScheduler runs
the missed job when it wakes up within an hour (`misfire_grace_time=3600`).

## Cron / no web app

If you prefer not to keep the server running, disable *Scheduled runs* in Settings and run from cron:

```
0 7 * * 1,4  cd /path/to/dealwatch && /opt/homebrew/bin/uv run dealwatch run >> data/logs/cron.log 2>&1
```

Reports land in `data/reports/` and in the UI whenever you start it.

## Backups and reset

Everything is in `data/dealwatch.db` (plus `data/reports/*.md`). Copy the file to back up. Delete it to reset;
retailers are re-seeded on next start, items are not.

## Updating

```bash
git pull && uv sync && uv run pytest
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "no API key" badge, Run now disabled | `.env` lacks `ANTHROPIC_API_KEY`; restart after adding it |
| Batch error `bad request: ... allowed_domains` | A retailer domain is invalid (scheme, wildcard, non-ASCII) - fix it on the Retailers page |
| Batch error `web search is not enabled` | Web search is disabled for your org in the Anthropic Console |
| Many `not_found` | Improve search hints (Dutch product name, pack size), enable more shops, or switch search mode to open |
| Run stuck in `running` | The process died mid-run; the next start shows no lock, just run again (rows stay for the record) |
| Costs higher than expected | Lower batch caps / effort in Settings; check the run page for the number of searches per batch |
