# Dealwatch on the Raspberry Pi

Dealwatch runs on the Pi `pi-apps` (192.168.54.46) next to ENDUR∆NCE and TOOL·E, at `https://dealwatch.home.arpa`,
reachable on the home network and its VPN only. The Pi, the proxy, the certificates and the trust on the Mac and
iPhone were set up for ENDUR∆NCE (its `docs/18-raspberry-pi.md`); nothing here changes them.

```
iPhone / Mac ──HTTPS──> Caddy (/srv/proxy, ports 80/443) ──> dealwatch:8000 (/srv/dealwatch, network "proxy")
                                                         ├─> toole:8000
                                                         └─> endurance:8000
```

On the Pi:

| Path | What |
|---|---|
| `/srv/dealwatch/compose.yaml`, `make-env.sh` | Sent by `deploy/ship.sh app` |
| `/srv/dealwatch/.env` | Secrets, mode 600, written only by `make-env.sh` |
| `/srv/dealwatch/data/` | `dealwatch.db` and `uploads/` (item photos) |
| `/srv/dealwatch/backups/` | Nightly archives at 03:00, newest 30 |
| `/srv/proxy/conf/sites/dealwatch.caddy` | Sent by `deploy/ship.sh site` |

The container runs as uid 1000 on a read-only file system with every capability dropped, publishes no port, and
is limited to 1 GB of memory (Claude Code takes about 300 MB while it runs).

## Jobs inside the app

| When | What |
|---|---|
| Every day at 02:30 Amsterdam time (Settings, Schedule, Start time) | Price check of the items that are due. Each item is still checked only as often as its priority says (need every 3 days, stock up weekly, wishlist every 2 weeks), so most days only a few items go out. A check missed while the app was down runs when it is back, within the hour. The log has "run N started" and "run N finished" in local time; the Checks page has the same |
| Every night at 03:00 | Backup of the database and photos into `backups/` |

## First setup

Steps marked **owner** need a password or a tap only the owner does.

1. **Folder (owner, once).** `/srv` belongs to root:

   ```bash
   ssh -t pi-apps 'sudo install -d -o amir -g amir -m 750 /srv/dealwatch /srv/dealwatch/data /srv/dealwatch/backups'
   ```

2. **Send the app.** On the Mac, in this folder: `make pi-ship`. It builds the image, sends it with
   `compose.yaml` and `make-env.sh`, and stops with "No .env yet" the first time.
3. **Login password (owner).**

   ```bash
   ssh -t pi-apps /srv/dealwatch/make-env.sh
   ```

   Twelve characters or more. It also makes the cookie's signing key.
4. **Start it.** `make pi-ship` again.
5. **Proxy site.** `make pi-site` adds `dealwatch.home.arpa` to Caddy and reloads it; the other apps keep running.
6. **DNS rule (owner).** In the Firewalla app: Home, Services, Custom DNS Rules, Add: domain `dealwatch.home.arpa`,
   IP `192.168.54.46`.
7. **Check.**

   ```bash
   curl https://dealwatch.home.arpa/healthz
   ```

   Then open it on the iPhone in Safari, sign in, and Share, Add to Home Screen. If the Mac says "no such name"
   right after adding the rule, it remembers the earlier failure for a while; meanwhile test with
   `curl --resolve dealwatch.home.arpa:443:192.168.54.46 https://dealwatch.home.arpa/healthz`.
8. **Claude (owner).** [assistant.md](assistant.md). Without it the app works, but no prices are checked.

## Updating

`make pi-ship`. Data, backups and `.env` stay; new tables and columns are added when the app starts.

## Day to day

| Task | Command |
|---|---|
| What is running, disk, memory | `make pi-status` |
| Back up now and copy all backups to the Mac | `make pi-backups` (into `backups/pi/`) |
| Logs | `ssh pi-apps 'cd /srv/dealwatch && docker compose logs --tail 50'` |
| New login password (owner) | `ssh -t pi-apps /srv/dealwatch/make-env.sh --replace` (every device signs in again) |

## Restore

`make pi-restore a=backups/pi/<archive>`. It checks the archive, keeps what was there, and starts the app again.
When and how, for the owner or an agent: [recovery.md](recovery.md).
