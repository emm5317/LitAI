import io
import zipfile
from email.message import EmailMessage

import pytest

from lpc.extract import ExtractError, docx_pages, extract, html_to_text
from lpc.ingest import ingest_file, search
from lpc.matters import create_matter, get_matter, matter_folder

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(body_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document {W}><w:body>{body_xml}</w:body></w:document>')
    return buf.getvalue()


def para(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def run(text: str) -> str:
    return f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>"


@pytest.fixture
def matter(conn, settings):
    mid = create_matter(conn, {"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee"},
                        settings.matters_dir)
    m = get_matter(conn, mid)
    return m, matter_folder(m, settings.matters_dir)


def test_docx_pages_follow_words_rendered_breaks():
    data = make_docx(
        para(run("Offer letter.")) +
        para("<w:r><w:lastRenderedPageBreak/><w:t>Page two: exempt status.</w:t></w:r>") +
        para("<w:r><w:delText>struck language</w:delText></w:r>", run("kept")) +
        "<w:tbl><w:tr><w:tc>" + para(run("Salary")) + "</w:tc><w:tc>" + para(run("$900")) + "</w:tc></w:tr></w:tbl>"
    )
    pages = docx_pages(data)
    assert len(pages) == 2
    assert pages[0] == "Offer letter."
    assert "exempt status" in pages[1] and "kept" in pages[1] and "struck" not in pages[1]
    assert "Salary" in pages[1] and "$900" in pages[1]


def test_docx_falls_back_to_hard_page_breaks():
    data = make_docx(para(run("One")) + para('<w:r><w:br w:type="page"/></w:r>') + para(run("Two")))
    assert docx_pages(data) == ["One", "Two"]


def _email(with_attachment=True, html=False) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Pat Manager <pat@acme.example>"
    msg["To"] = "HR <hr@acme.example>"
    msg["Cc"] = "Legal <legal@acme.example>"
    msg["Subject"] = "Re: Doe schedule change"
    msg["Date"] = "Tue, 03 Mar 2026 09:15:00 -0600"
    if html:
        msg.set_content("<html><head><style>p{color:red}</style></head><body><p>Move Doe to the"
                        " &amp; night shift.</p><script>x()</script></body></html>", subtype="html")
    else:
        msg.set_content("Move Doe to the night shift after the complaint.")
        msg.add_alternative("<p>Move Doe to the night shift after the complaint.</p>", subtype="html")
    if with_attachment:
        msg.add_attachment(b"Shift roster: Doe moved to nights.", maintype="text", subtype="plain",
                           filename="roster.txt")
        msg.add_attachment(b"\x89PNG fake logo", maintype="image", subtype="png", filename="logo.png",
                           disposition="inline", cid="<logo>")
    return msg.as_bytes()


def test_eml_is_indexed_with_headers_and_attachments(conn, matter, tmp_path):
    m, root = matter
    data = _email()                       # MIME boundaries are random, so build it once
    src = tmp_path / "schedule.eml"
    src.write_bytes(data)
    doc = ingest_file(conn, m, root, src)
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc.id,)).fetchone()
    assert row["title"] == "Re: Doe schedule change" and row["doc_type"] == "email"
    assert row["doc_date"] == "2026-03-03" and "pat@acme.example" in row["author"]
    assert "hr@acme.example" in row["recipients"] and "legal@acme.example" in row["recipients"]
    assert [h["document_id"] for h in search(conn, "complaint", m["id"])] == [doc.id]

    children = conn.execute("SELECT * FROM documents WHERE parent_id = ?", (doc.id,)).fetchall()
    assert [c["title"] for c in children] == ["roster"]                 # the inline logo is skipped
    assert (root / "10 Source" / "schedule - attachments" / "roster.txt").is_file()
    assert search(conn, "roster", m["id"])[0]["document_id"] == children[0]["id"]

    # the same email again: nothing new, and the attachment is not written twice
    again = tmp_path / "copy.eml"
    again.write_bytes(data)
    assert ingest_file(conn, m, root, again) == (doc.id, False)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_html_only_email_body_is_plain_text(tmp_path):
    src = tmp_path / "x.eml"
    src.write_bytes(_email(with_attachment=False, html=True))
    page = extract(src).pages[0]
    assert "Move Doe to the & night shift." in page
    assert "color:red" not in page and "x()" not in page and "<p>" not in page


def test_html_to_text_keeps_line_breaks():
    assert html_to_text("<div>One</div><div>Two&nbsp;three</div>") == "One\n\nTwo three"


def test_outlook_msg(conn, matter, tmp_path, monkeypatch):
    import datetime as dt

    import extract_msg

    class FakeAttachment:
        def __init__(self, name, data, hidden=False):
            self.longFilename, self.shortFilename, self.data, self.hidden = name, None, data, hidden

    class FakeMsg:
        subject = "Final warning - Doe"
        sender = "Pat Manager <pat@acme.example>"
        to = "Jane Doe <jane@example.com>"
        cc = None
        date = dt.datetime(2026, 2, 10, 14, 0)
        body = "This is your final warning about attendance."
        htmlBody = None
        attachments = [FakeAttachment("warning.txt", b"Written warning: attendance."),
                       FakeAttachment("image001.png", b"logo", hidden=True)]
        closed = False

        def close(self):
            FakeMsg.closed = True

    monkeypatch.setattr(extract_msg, "openMsg", lambda path: FakeMsg())
    m, root = matter
    src = tmp_path / "warning.msg"
    src.write_bytes(b"not really an Outlook file")
    doc = ingest_file(conn, m, root, src)
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc.id,)).fetchone()
    assert row["title"] == "Final warning - Doe" and row["doc_type"] == "email" and row["doc_date"] == "2026-02-10"
    assert search(conn, "attendance warning", m["id"])
    assert [c["title"] for c in conn.execute("SELECT title FROM documents WHERE parent_id = ?", (doc.id,))] == ["warning"]
    assert FakeMsg.closed


def test_unreadable_files_are_recorded_not_fatal(conn, matter, tmp_path):
    import pymupdf as fitz

    m, root = matter
    bad = tmp_path / "memo.docx"
    bad.write_bytes(b"PK\x03\x04 this is not a real docx")
    doc = ingest_file(conn, m, root, bad)
    row = conn.execute("SELECT text_status, notes FROM documents WHERE id = ?", (doc.id,)).fetchone()
    assert row["text_status"] == "failed" and "Could not read the text" in row["notes"]

    locked = tmp_path / "locked.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "secret")
    pdf.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(ExtractError, match="password"):
        extract(locked)


def test_unsupported_types_have_no_text(tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"\xff\xd8")
    assert extract(src).pages == []


def test_attachment_already_in_matter_is_not_copied_again(conn, matter, tmp_path):
    m, root = matter
    first, second = tmp_path / "a.eml", tmp_path / "b.eml"
    first.write_bytes(_email())
    data = _email().replace(b"Re: Doe schedule change", b"Fwd: Doe schedule change")
    second.write_bytes(data)
    ingest_file(conn, m, root, first)
    doc = ingest_file(conn, m, root, second)
    assert doc.added
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE parent_id = ?", (doc.id,)).fetchone()[0] == 0
    assert len(list(root.rglob("roster*.txt"))) == 1
