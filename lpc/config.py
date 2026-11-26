"""Paths and settings.

On Windows the defaults are:
  data (database, logs): %LOCALAPPDATA%\\LawPracticeConsole
  matter folders:        %USERPROFILE%\\Documents\\Law Practice Console\\Matters

Override either with the LPC_DATA_DIR and LPC_MATTERS_DIR environment variables
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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lpc.sqlite3"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("LPC_DATA_DIR") or user_data_dir(APP_NAME, appauthor=False))
    matters_dir = Path(
        os.environ.get("LPC_MATTERS_DIR")
        or Path(user_documents_dir()) / "Law Practice Console" / "Matters"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    matters_dir.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=data_dir, matters_dir=matters_dir)
