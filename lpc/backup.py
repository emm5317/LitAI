"""Daily database backup.

Once a day the whole database is copied with SQLite's VACUUM INTO, which makes a clean,
compact copy even while the app is in use. Copies are named lpc-YYYY-MM-DD.sqlite3 and the
newest 14 are kept.

To restore: close the app, delete lpc.sqlite3 (and any lpc.sqlite3-wal / -shm files) in the
data folder, copy a backup there, and rename it lpc.sqlite3.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import time
from pathlib import Path

KEEP = 14


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups, oldest first."""
    return sorted(backup_dir.glob("lpc-*.sqlite3")) if backup_dir.is_dir() else []


def backup_database(db_path: Path, backup_dir: Path, *, keep: int = KEEP,
                    today: dt.date | None = None) -> Path | None:
    """Make today's backup if there is none yet. Returns the new file, or None if skipped."""
    today = today or dt.date.today()
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"lpc-{today.isoformat()}.sqlite3"
    if dest.exists():
        return None
    partial = dest.with_suffix(".partial")      # renamed only once complete
    partial.unlink(missing_ok=True)
    conn = sqlite3.connect(str(db_path))         # its own connection, so it never blocks the app
    try:
        conn.execute("VACUUM INTO ?", (str(partial),))
    finally:
        conn.close()
    partial.replace(dest)
    for old in list_backups(backup_dir)[:-keep]:
        old.unlink()
    return dest


def start_daily_backups(db_path: Path, backup_dir: Path) -> threading.Thread:
    """Back up now, then check every hour (the app may stay open for days)."""
    def loop() -> None:
        while True:
            try:
                backup_database(db_path, backup_dir)
            except Exception as exc:  # a failed backup must never stop the app
                print(f"Backup failed: {exc}")
            time.sleep(3600)

    thread = threading.Thread(target=loop, name="daily-backup", daemon=True)
    thread.start()
    return thread
