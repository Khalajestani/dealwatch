"""Consistent backup of the database and the item photos into one archive. Safe while the app is running.

The app makes one every night at 03:00 and keeps the newest DEALWATCH_BACKUPS_KEEP (30).
By hand: dealwatch-backup [DEST_DIR] [--keep N]   (default DEST_DIR: DEALWATCH_BACKUPS_DIR, "backups")
"""

from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dest", nargs="?", type=Path, help="directory for the archives")
    parser.add_argument("--keep", type=int, help="number of archives to keep (default DEALWATCH_BACKUPS_KEEP, 30)")
    args = parser.parse_args()
    try:
        print(backup(Env(), args.dest, args.keep))
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
