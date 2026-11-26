"""Paths and settings.

On Windows the defaults are:
  data (database, logs): %LOCALAPPDATA%\\LawPracticeConsole        (stays on this machine)
  matter folders:        %OneDrive%\\Law Practice Console\\Matters
  database backups:      %OneDrive%\\Law Practice Console\\Backups

OneDrive is used when Windows reports a OneDrive folder (the work account, OneDriveCommercial,
is preferred over a personal one). Without OneDrive, matter folders go under Documents and
backups go under the data folder.

Override any of them with LPC_DATA_DIR, LPC_MATTERS_DIR and LPC_BACKUP_DIR
(the tests do this so they never touch real data).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_documents_dir

APP_NAME = "LawPracticeConsole"
PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    matters_dir: Path
    backup_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lpc.sqlite3"


def onedrive_root() -> Path | None:
    """The OneDrive folder Windows reports, if any (work account first)."""
    for var in ("OneDriveCommercial", "OneDrive"):
        value = os.environ.get(var)
        if value and Path(value).is_dir():
            return Path(value)
    return None


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("LPC_DATA_DIR") or user_data_dir(APP_NAME, appauthor=False))
    cloud = onedrive_root()
    home = cloud / "Law Practice Console" if cloud else Path(user_documents_dir()) / "Law Practice Console"
    matters_dir = Path(os.environ.get("LPC_MATTERS_DIR") or home / "Matters")
    backup_dir = Path(os.environ.get("LPC_BACKUP_DIR")
                      or (home / "Backups" if cloud else data_dir / "Backups"))
    data_dir.mkdir(parents=True, exist_ok=True)
    matters_dir.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=data_dir, matters_dir=matters_dir, backup_dir=backup_dir)
