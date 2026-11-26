import datetime as dt
import sqlite3

from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.backup import backup_database, list_backups
from lpc.config import load_settings
from lpc.matters import create_matter


def test_backup_copies_database_once_a_day(conn, settings):
    create_matter(conn, {"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee"},
                  settings.matters_dir)
    day = dt.date(2026, 11, 26)
    made = backup_database(settings.db_path, settings.backup_dir, today=day)
    assert made and made.name == "lpc-2026-11-26.sqlite3"
    copy = sqlite3.connect(made)
    assert copy.execute("SELECT short_name FROM matters").fetchone()[0] == "Doe v. Acme"
    copy.close()
    assert backup_database(settings.db_path, settings.backup_dir, today=day) is None   # already done today


def test_backup_keeps_only_the_newest(conn, settings):
    for n in range(5):
        backup_database(settings.db_path, settings.backup_dir, keep=3, today=dt.date(2026, 11, 1 + n))
    assert [p.name for p in list_backups(settings.backup_dir)] == [
        "lpc-2026-11-03.sqlite3", "lpc-2026-11-04.sqlite3", "lpc-2026-11-05.sqlite3"]
    assert not list(settings.backup_dir.glob("*.partial"))


def test_dashboard_shows_backup_status(settings):
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    assert "No database backup yet" in client.get("/").text
    backup_database(settings.db_path, settings.backup_dir, today=dt.date(2026, 11, 26))
    assert "Last database backup: 2026-11-26" in client.get("/").text


def test_onedrive_is_the_default_home(tmp_path, monkeypatch):
    cloud = tmp_path / "OneDrive - Firm"
    cloud.mkdir()
    monkeypatch.setenv("LPC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LPC_MATTERS_DIR", raising=False)
    monkeypatch.delenv("LPC_BACKUP_DIR", raising=False)
    monkeypatch.setenv("OneDriveCommercial", str(cloud))
    s = load_settings()
    assert s.matters_dir == cloud / "Law Practice Console" / "Matters"
    assert s.backup_dir == cloud / "Law Practice Console" / "Backups"
    assert s.db_path.parent == tmp_path / "data"           # the live database never syncs
