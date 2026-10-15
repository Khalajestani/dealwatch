# Recovery

What to do when data is lost, wrong or damaged, and how to put it back after a fix. Written so the owner can say
to an agent: "follow docs/recovery.md to ...". The backup and restore code is `src/dealwatch/backup.py`.

## What is backed up

| What | Where on the Pi | In the backups |
|---|---|---|
| The database | `/srv/dealwatch/data/dealwatch.db` | yes |
| Item photos | `/srv/dealwatch/data/uploads/` | yes |
| Secrets | `/srv/dealwatch/.env` | no: the owner types them again with `make-env.sh` |
| The code | git | no |

An archive is `dealwatch-YYYYMMDD-HHMMSS.tar.gz` holding `dealwatch.db` and `uploads/`, laid out exactly as in `data/`. The app
writes one every night at 03:00 and keeps the newest 30 on the Pi. `make pi-backups` writes a fresh one and copies all
of them to `backups/pi/` in this repo on the Mac (git-ignored), where nothing is ever pruned. Until that runs the only
copies are on the Pi's SD card, so run it weekly and before anything risky.

## Commands (on the Mac, in this repo)

| Do | Command |
|---|---|
| Back up now on the Pi and copy every backup to the Mac | `make pi-backups` |
| Put a backup back on the Pi | `make pi-restore a=backups/pi/dealwatch-YYYYMMDD-HHMMSS.tar.gz` |
| Put a backup back in the local app (stop `make dev` first) | `make restore a=<archive>` |
| A backup of the local app | `make backup` |
| Look inside one without touching anything | `mkdir -p /tmp/look && tar -xzf <archive> -C /tmp/look && sqlite3 /tmp/look/dealwatch.db` |
| The app's log on the Pi | `ssh pi-apps 'cd /srv/dealwatch && docker compose logs --tail 100'` |

A restore first checks the archive: it must hold a whole SQLite database and nothing that unpacks outside its
folder. A refused archive changes nothing. Then it keeps what was there as a new archive in `backups/` (the name is
printed), swaps the database and uploads/ with the app stopped, and starts the app again. An older backup is fine:
the app adds the columns an older database lacks when it starts.

## Rules for an agent

1. Start with `make pi-backups`. It captures the state before anything else happens, and every later step can be
   undone from it.
2. Never change the database on the Pi in place. Work on a copy on the Mac, then restore it.
3. Code before data. If the fix needs a code or schema change, release it first (`make pi-ship`), then restore.
   Never put back a backup made by newer code than what runs.
4. Whatever is written on the Pi between the copy and the restore is lost. Do the fix and the restore in one go, and
   ask the owner not to use the app meanwhile.
5. Afterwards open the app and check what was asked for, then `make pi-backups` again so the Mac also holds the
   archive of what was replaced.
6. Report which archive was put back and the name of the archive of what was there before.

## Cases

**Something is wrong or missing (a bug, a wrong edit, a mistake by the assistant).** `make pi-backups`, then find
the newest archive from before the problem (the names are dates and times) and confirm in it, with sqlite3, that
the rows are right there. If nothing worth keeping happened since, `make pi-restore a=<that archive>`. Otherwise fix
the current data instead: unpack the newest archive into a scratch folder, copy the lost or corrected rows from the
older one with sqlite3 (`ATTACH '<older>/dealwatch.db' AS old;` then `INSERT ... SELECT` or `UPDATE` from `old`), pack it
again with `tar -czf fixed.tar.gz -C <scratch> dealwatch.db uploads` and `make pi-restore a=fixed.tar.gz`. To try the fixed
data first, `make restore a=fixed.tar.gz` and `make dev`.

**The app does not start after a release.** Read the log. If the code is at fault, fix it and `make pi-ship`. If the
data is, put back the newest archive from before the release.

**The SD card died or the Pi is replaced.** Set the Pi up again (ENDURANCE's `docs/18-raspberry-pi.md` for the Pi, then [raspberry-pi.md](raspberry-pi.md)), let the owner run `make-env.sh` for the
secrets, `make pi-ship`, then `make pi-restore a=<newest archive in backups/pi>`.

**A change needs the old data in a new shape.** That is a migration, not a restore: write it in the code
(`db.init_db` adds nullable columns; anything more needs a real migration step), try it on a copy (`make restore a=<archive>`, then `make dev`), then release it.
