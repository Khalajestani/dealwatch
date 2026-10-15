"""Consistent backup of the database and the item photos into one archive. Safe while the app is running.

The app makes one every night at 03:00 and keeps the newest DEALWATCH_BACKUPS_KEEP (30).
By hand: dealwatch-backup [DEST_DIR] [--keep N]   (default DEST_DIR: DEALWATCH_BACKUPS_DIR, "backups")
Put one back: dealwatch-backup --restore ARCHIVE (stop the app first). See docs/recovery.md.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Env


def backup(env: Env, dest: Path | None = None, keep: int | None = None, timezone: str = "Europe/Amsterdam") -> Path:
    """The archive is named after the local time in `timezone` (the container's clock is UTC)."""
    dest = dest or env.backups_dir
    keep = keep or env.backups_keep
    if not env.db_path.exists():
        raise FileNotFoundError(f"{env.db_path} not found.")
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"dealwatch-{datetime.now(ZoneInfo(timezone)):%Y%m%d-%H%M%S}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "dealwatch.db"
        with closing(sqlite3.connect(env.db_path)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)  # online backup API: consistent even during writes
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="dealwatch.db")
            if env.uploads_dir.exists():
                tar.add(env.uploads_dir, arcname="uploads")

    for old in sorted(dest.glob("dealwatch-*.tar.gz"))[:-keep]:
        old.unlink()
    return archive


def restore(env: Env, archive: Path) -> Path | None:
    """Put an archive's database and photos in place of the current ones. The app must be stopped;
    it adds the columns an older database lacks when it starts. What was there is backed up first: returns that archive, None
    when there was no database."""
    db = env.db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=db.parent) as tmp:
        unpacked = Path(tmp)
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter="data")  # nothing lands outside tmp
        check_database(unpacked / db.name)
        before = backup(env) if db.exists() else None
        for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            path.unlink(missing_ok=True)
        shutil.rmtree(env.uploads_dir, ignore_errors=True)
        (unpacked / db.name).replace(db)
        if (unpacked / "uploads").is_dir():
            shutil.move(unpacked / "uploads", env.uploads_dir)
    return before


def check_database(db: Path) -> None:
    """Raise ValueError unless db is a whole SQLite database."""
    if not db.is_file():
        raise ValueError(f"The archive has no {db.name}.")
    try:
        with closing(sqlite3.connect(db)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"{db.name} in the archive is not a database: {exc}") from None
    if result != "ok":
        raise ValueError(f"{db.name} in the archive is damaged: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dest", nargs="?", type=Path, help="directory for the archives")
    parser.add_argument("--keep", type=int, help="number of archives to keep (default DEALWATCH_BACKUPS_KEEP, 30)")
    parser.add_argument("--restore", type=Path, metavar="ARCHIVE", help="put this archive back (stop the app first)")
    args = parser.parse_args()
    if args.restore:
        try:
            before = restore(Env(), args.restore)
        except tarfile.TarError:
            raise SystemExit(f"Not restored: {args.restore.name} is not a backup archive.") from None
        except (ValueError, OSError) as exc:
            raise SystemExit(f"Not restored: {exc}") from None
        print(f"Restored {args.restore.name}. What was there before: {before or 'nothing'}")
        return
    try:
        print(backup(Env(), args.dest, args.keep))
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
