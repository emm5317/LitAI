import pytest

from lpc.ingest import _fts_query, ingest_file, search, search_titles
from lpc.matters import create_matter, get_matter, matter_folder


@pytest.fixture
def docs(conn, settings, tmp_path):
    mid = create_matter(conn, {"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee"},
                        settings.matters_dir)
    m = get_matter(conn, mid)
    root = matter_folder(m, settings.matters_dir)
    ids = {}
    for name, text, meta in [
        ("schedule", "Doe was moved to the night shift after she complained.", {"doc_date": "2026-03-03", "source": "client"}),
        ("roster", "Shift roster. Doe works nights and weekends.", {"doc_date": "2026-04-01", "source": "opposing"}),
        ("handbook", "Overtime must be approved. Exempt employees are paid a salary.", {"doc_date": "2025-01-01"}),
        ("payroll", "Overtime hours: 52. Non-exempt.", {"doc_date": "2026-05-01", "key_document": 1}),
    ]:
        src = tmp_path / f"{name}.txt"
        src.write_text(text)
        ids[name] = ingest_file(conn, m, root, src).id
        sets = ", ".join(f"{k} = ?" for k in meta)
        conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*meta.values(), ids[name]))
    photo = tmp_path / "Doe badge photo.jpg"                     # no text at all
    photo.write_bytes(b"\xff\xd8")
    ids["photo"] = ingest_file(conn, m, root, photo).id
    conn.commit()
    return mid, ids


def titles_of(rows):
    return sorted(r["title"] for r in rows)


def test_fts_query_translation():
    assert _fts_query("night shift") == '"night" "shift"'
    assert _fts_query('"night shift" overtim*') == '"night shift" "overtim"*'
    assert _fts_query("nights OR weekends") == '"nights" OR "weekends"'
    assert _fts_query("overtime -exempt") == '"overtime" NOT "exempt"'
    assert _fts_query("-exempt") == ""                      # NOT needs something before it
    assert _fts_query("OR overtime OR") == '"overtime"'
    assert _fts_query('quote"injection OR') == '"quoteinjection"'
    assert _fts_query('"unbalanced phrase') == '"unbalanced" "phrase"'


def test_phrase_or_and_not(conn, docs):
    mid, _ = docs
    assert titles_of(search(conn, "doe shift", mid)) == ["roster", "schedule"]
    assert titles_of(search(conn, '"night shift"', mid)) == ["schedule"]
    assert titles_of(search(conn, "weekends OR complained", mid)) == ["roster", "schedule"]
    assert titles_of(search(conn, "overtime", mid)) == ["handbook", "payroll"]
    assert titles_of(search(conn, "overtime -salary", mid)) == ["payroll"]
    assert titles_of(search(conn, "overtim*", mid)) == ["handbook", "payroll"]


def test_filters(conn, docs):
    mid, _ = docs
    assert titles_of(search(conn, "doe", mid, source="client")) == ["schedule"]
    assert titles_of(search(conn, "overtime", mid, date_from="2026-01-01")) == ["payroll"]
    assert titles_of(search(conn, "doe", mid, date_to="2026-03-31")) == ["schedule"]
    assert titles_of(search(conn, "overtime", mid, key_only=True)) == ["payroll"]
    assert titles_of(search(conn, "doe", mid, doc_type="email")) == []


def test_title_matches_find_files_without_text(conn, docs):
    mid, ids = docs
    assert search(conn, "badge", mid) == []
    rows = search_titles(conn, "badge photo", mid)
    assert [r["document_id"] for r in rows] == [ids["photo"]]
    assert search_titles(conn, "badge -photo OR", mid)[0]["document_id"] == ids["photo"]   # operators ignored


def test_search_page_with_filters(settings, docs):
    from fastapi.testclient import TestClient

    from lpc.app import create_app

    mid, _ = docs
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    r = client.get("/search", params={"q": "doe", "matter_id": mid, "source": "opposing"})
    assert "roster" in r.text and "schedule" not in r.text
    r = client.get("/search", params={"q": "badge", "matter_id": mid})
    assert "Titles" in r.text and "Doe badge photo" in r.text
    r = client.get("/search", params={"q": "doe", "date_from": "soon"}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "from date" in r.text
