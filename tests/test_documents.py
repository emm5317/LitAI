from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.ingest import ingest_file
from lpc.matters import get_matter, matter_folder

HX = {"HX-Request": "true"}


@pytest.fixture
def client(settings):
    c = TestClient(create_app(settings), base_url="http://127.0.0.1")
    r = c.post("/matters", data={"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee"},
               follow_redirects=False)
    c.mid = int(r.headers["location"].rsplit("/", 1)[1])
    c.conn = c.app.state.conn
    m = get_matter(c.conn, c.mid)
    c.add = lambda path, **kw: ingest_file(c.conn, m, matter_folder(m, settings.matters_dir), path, **kw).id
    return c


def make_pdf(path: Path, pages: list[str]) -> Path:
    import pymupdf as fitz

    pdf = fitz.open()
    for text in pages:
        pdf.new_page().insert_text((72, 72), text)
    pdf.save(path)
    return path


def test_document_page_shows_escaped_text_with_search_marks(client, tmp_path):
    src = tmp_path / "memo.txt"
    src.write_text("<script>alert(1)</script> Doe was moved to nights after complaining.")
    doc = client.add(src)
    page = client.get(f"/documents/{doc}?q=complaining").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<mark>complaining</mark>" in page
    assert "View PDF" not in page                      # not a PDF


def test_search_hits_link_to_the_page(client, tmp_path):
    doc = client.add(make_pdf(tmp_path / "complaint.pdf", ["COUNT I overtime", "COUNT II retaliation"]))
    r = client.get("/search", params={"q": "retaliation"})
    assert f"/documents/{doc}?page=2&q=retaliation#p-2" in r.text
    assert f"/documents/{doc}/file#page=2" in r.text
    page = client.get(f"/documents/{doc}?page=2&q=retaliation").text
    assert 'class="page current" id="p-2"' in page and "View PDF at p. 2" in page


def test_long_documents_show_a_window_of_pages(client, tmp_path):
    doc = client.add(make_pdf(tmp_path / "production.pdf", [f"Bates page {n}" for n in range(1, 121)]))
    page = client.get(f"/documents/{doc}?page=70").text
    assert 'id="p-65"' in page and 'id="p-114"' in page
    assert 'id="p-64"' not in page and 'id="p-115"' not in page
    assert "Earlier pages" in page and "Later pages" in page


def test_file_is_served_inline_and_stays_inside_the_matter(client, tmp_path):
    doc = client.add(make_pdf(tmp_path / "order.pdf", ["ORDER"]))
    r = client.get(f"/documents/{doc}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline") and r.content.startswith(b"%PDF")

    client.conn.execute("UPDATE documents SET file_path = '../../secret.txt' WHERE id = ?", (doc,))
    assert client.get(f"/documents/{doc}/file").status_code == 403
    client.conn.execute("UPDATE documents SET file_path = '10 Source/gone.pdf' WHERE id = ?", (doc,))
    r = client.get(f"/documents/{doc}/file")
    assert r.status_code == 404 and "missing" in r.text


def test_open_and_reveal_use_the_os(client, tmp_path, monkeypatch):
    import lpc.app

    calls = []
    monkeypatch.setattr(lpc.app, "open_in_os", lambda path, reveal=False: calls.append((path.name, reveal)))
    doc = client.add(make_pdf(tmp_path / "order.pdf", ["ORDER"]))
    assert client.post(f"/documents/{doc}/open").status_code == 204
    assert client.post(f"/documents/{doc}/reveal").status_code == 204
    assert calls == [("order.pdf", False), ("order.pdf", True)]

    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ")
    bad = client.add(exe)
    r = client.post(f"/documents/{bad}/open", headers=HX)
    assert r.status_code == 400 and "is a program" in r.text and len(calls) == 2


def test_edit_document_details(client, tmp_path):
    src = tmp_path / "letter.txt"
    src.write_text("Termination letter")
    doc = client.add(src)
    form = {"title": "Termination letter", "doc_type": "correspondence", "source": "client",
            "doc_date": "2026-01-15", "bates_begin": "ACME000101", "key_document": "1"}
    r = client.post(f"/documents/{doc}/edit", data=form, follow_redirects=False)
    assert r.status_code == 303
    row = client.conn.execute("SELECT * FROM documents WHERE id = ?", (doc,)).fetchone()
    assert (row["doc_type"], row["doc_date"], row["key_document"], row["privileged"]) == ("correspondence", "2026-01-15", 1, 0)
    log = client.conn.execute("SELECT summary FROM changelog ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert log.startswith("Updated document Termination letter:") and "key document" in log
    assert "key</span>" in client.get(f"/matters/{client.mid}/documents").text

    assert client.post(f"/documents/{doc}/edit", data={**form, "doc_date": "Jan 15"}).status_code == 400
    assert client.post(f"/documents/{doc}/edit", data={**form, "doc_type": "napkin"}).status_code == 400
    assert client.post(f"/documents/{doc}/edit", data={**form, "title": " "}).status_code == 400


def test_email_page_links_its_attachments(client, tmp_path):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Roster", "a@example.com", "b@example.com"
    msg.set_content("See attached.")
    msg.add_attachment(b"Doe: nights", maintype="text", subtype="plain", filename="roster.txt")
    src = tmp_path / "roster.eml"
    src.write_bytes(msg.as_bytes())
    doc = client.add(src)
    child = client.conn.execute("SELECT id FROM documents WHERE parent_id = ?", (doc,)).fetchone()[0]
    assert f'href="/documents/{child}">roster' in client.get(f"/documents/{doc}").text
    assert f'attached to <a href="/documents/{doc}">Roster' in client.get(f"/documents/{child}").text
