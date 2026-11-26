import pytest

from lpc.config import Settings
from lpc.db import open_database


@pytest.fixture
def settings(tmp_path, monkeypatch):
    data, matters = tmp_path / "data", tmp_path / "matters"
    data.mkdir()
    matters.mkdir()
    monkeypatch.setenv("LPC_DATA_DIR", str(data))
    monkeypatch.setenv("LPC_MATTERS_DIR", str(matters))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Settings(data_dir=data, matters_dir=matters)


@pytest.fixture
def conn(settings):
    c = open_database(settings.db_path)
    yield c
    c.close()
