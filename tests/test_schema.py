import sqlite3
from pathlib import Path

import pytest

from lpc.db import migrate
from lpc.ingest import ingest_file, search
from lpc.matters import (
    MATTER_FOLDERS, MatterError, add_alias, add_party_to_matter, conflicts_search, create_matter, get_matter,
    list_matters, matter_folder, normalize_name,
)


def _matter(conn, settings, **over):
    data = {"short_name": "Doe v. Acme", "caption": "Jane Doe v. Acme Logistics, Inc.", "side": "employer",
            "forum_type": "federal_district", "rules_profile": "ndil", "status": "active"}
    data.update(over)
    return create_matter(conn, data, settings.matters_dir)


def test_migrations_are_idempotent(conn):
    assert migrate(conn) == []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    for t in ("matters", "parties", "matter_parties", "claims", "events", "documents", "deadlines",
              "tasks", "discovery_sets", "conflict_checks", "ai_calls", "changelog", "v_matter_dashboard"):
        assert t in tables


def test_side_is_required_and_constrained(conn, settings):
    with pytest.raises(sqlite3.IntegrityError):
        _matter(conn, settings, side="neutral")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO matters (short_name, caption) VALUES ('x', 'y')")


def test_no_billing_tables(conn):
    names = " ".join(r[0] for r in conn.execute("SELECT name FROM sqlite_master"))
    for word in ("invoice", "trust", "ledger", "billing", "time_entr"):
        assert word not in names


def test_create_matter_makes_folders_and_logs(conn, settings):
    mid = _matter(conn, settings)
    m = get_matter(conn, mid)
    assert m["folder_path"] == "Doe v. Acme"                      # stored relative to the matters root
    for sub in MATTER_FOLDERS:
        assert (settings.matters_dir / "Doe v. Acme" / sub).is_dir()
    assert conn.execute("SELECT COUNT(*) FROM changelog WHERE matter_id = ?", (mid,)).fetchone()[0] == 1
    assert list_matters(conn)[0]["short_name"] == "Doe v. Acme"


def test_normalize_name():
    assert normalize_name("ACME Logistics, Inc.") == "acme logistics"
    assert normalize_name("The Acme Logistics Corporation") == "acme logistics"


def test_conflicts_find_variants_and_aliases(conn, settings):
    mid = _matter(conn, settings)
    pid = add_party_to_matter(conn, mid, "Acme Logistics, Inc.", "client", kind="organization")
    add_alias(conn, pid, "Roadrunner Freight", kind="dba")
    add_party_to_matter(conn, mid, "Jane Doe", "adverse_party")
    conn.commit()

    hits = conflicts_search(conn, ["ACME Logistics Corporation"])
    assert hits and hits[0]["role"] == "client" and hits[0]["score"] == 1.0
    assert conflicts_search(conn, ["Roadrunner Freight LLC"])[0]["matched_alias"] == "Roadrunner Freight"
    assert conflicts_search(conn, ["Jane Q. Doe"])
    assert conflicts_search(conn, ["Globex Corporation"]) == []


def test_ingest_and_search(conn, settings, tmp_path):
    mid = _matter(conn, settings)
    src = tmp_path / "offer_letter.txt"
    src.write_text("The employee will be classified as exempt and paid a weekly salary.", encoding="utf-8")
    m = get_matter(conn, mid)
    root = matter_folder(m, settings.matters_dir)
    doc_id = ingest_file(conn, m, root, src, doc_type="contract")
    assert doc_id.added and ingest_file(conn, m, root, src) == (doc_id.id, False)   # same hash, no duplicate
    assert (root / "10 Source" / "offer_letter.txt").is_file()
    hits = search(conn, "exempt salary", mid)
    assert hits and hits[0]["title"] == "offer_letter" and hits[0]["page_no"] == 1
    assert "\x02" in hits[0]["snippet"]
    assert search(conn, 'quote"injection OR') == []


def test_pdf_ingest_pages(conn, settings, tmp_path):
    import pymupdf as fitz

    pdf_path = tmp_path / "complaint.pdf"
    pdf = fitz.open()
    for text in ("COUNT I unpaid overtime under the FLSA", "COUNT II Illinois Minimum Wage Law"):
        page = pdf.new_page()
        page.insert_text((72, 72), text)
    pdf.save(pdf_path)
    mid = _matter(conn, settings)
    m = get_matter(conn, mid)
    ingest_file(conn, m, matter_folder(m, settings.matters_dir), pdf_path, doc_type="complaint")
    hit = search(conn, "Minimum Wage", mid)[0]
    assert hit["page_no"] == 2


def test_duplicate_names_and_folder_collisions_are_refused(conn, settings):
    _matter(conn, settings)
    with pytest.raises(MatterError, match="already exists"):
        _matter(conn, settings)
    with pytest.raises(MatterError, match="already used"):
        _matter(conn, settings, short_name="DOE V. ACME")           # same folder on Windows
    _matter(conn, settings, short_name="Roe: v Acme")
    with pytest.raises(MatterError, match="already used"):
        _matter(conn, settings, short_name="Roe/ v Acme")           # both become "Roe- v Acme"
    with pytest.raises(MatterError, match="letter or number"):
        _matter(conn, settings, short_name="???")


def test_old_absolute_folder_paths_still_resolve(tmp_path, settings):
    legacy = {"folder_path": str(tmp_path / "elsewhere" / "Old Matter")}
    assert matter_folder(legacy, settings.matters_dir) == tmp_path / "elsewhere" / "Old Matter"
