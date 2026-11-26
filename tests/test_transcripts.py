"""Transcript page:line parsing. The PDFs are built here to copy the layouts of real reporters'
transcripts (checked against two public court filings): label at the top right, line numbers
right-aligned in a left margin, ECF stamps, a label drawn after the text, condensed four-up sheets,
and a word index at the back. All names are fictional."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app, mark_terms, plain_terms, source_href
from lpc.ingest import ingest_file, search
from lpc.matters import create_matter, get_matter, matter_folder
from lpc.transcripts import cite, parse_transcript, quote_cite

TESTIMONY = {
    10: ["Q And when did you start at Fictional", "Logistics?", "A In January 2025.",
         "Q Who was your supervisor?", "A Pat Rivera, the shift lead."],
    11: ["Q Did you ever complain about overtime?", "A Yes. I e-mailed human resources on",
         "March 3, 2026, about unpaid hours."],
    12: ["Q What happened after that?", "A A week later Pat Rivera moved me to",
         "the night shift. Nobody explained why."],
    13: ["Q Did you object?", "A I objected in writing the same day."],
}


def full_size_page(pdf, page_no, lines, n_lines=25, label_after_text=False, ecf=None):
    page = pdf.new_page(width=612, height=792)
    if ecf:
        page.insert_text((97, 22), ecf, fontsize=9)
    if not label_after_text:
        page.insert_text((510, 62), f"Page {page_no}", fontsize=11)
    for i in range(n_lines):
        y = 84 + i * 25.4
        num = str(i + 1)
        page.insert_text((60 - 6 * len(num), y), num, fontsize=11)        # right-aligned numbers
        if i < len(lines):
            page.insert_text((99, y), lines[i], fontsize=11)
    if label_after_text:            # one reporter's files list the label after the testimony
        page.insert_text((467, 62), f"Page {page_no}", fontsize=11)
    page.insert_text((197, 740), "Fictional Reporting Services", fontsize=9)


def condensed_sheet(pdf, page_numbers, texts):
    page = pdf.new_page(width=612, height=792)
    page.insert_text((40, 20), "Rivera v. Fictional Logistics   Jordan Lee   March 3, 2027", fontsize=8)
    quadrants = [(30, 40), (30, 420), (320, 40), (320, 420)]     # left column top/bottom, then right
    for (x0, y0), page_no in zip(quadrants, page_numbers):
        page.insert_text((x0 + 200, y0 + 12), f"Page {page_no}", fontsize=8)
        for i in range(25):
            y = y0 + 30 + i * 13.5
            num = str(i + 1)
            page.insert_text((x0 + 12 - 4 * len(num), y), num, fontsize=7)
            lines = texts.get(page_no, [])
            if i < len(lines):
                page.insert_text((x0 + 22, y), lines[i], fontsize=7)
    page.insert_text((250, 780), f"({page_numbers[0] // 4}) Pages {page_numbers[0]} - {page_numbers[-1]}", fontsize=8)


def word_index_page(pdf, page_no):
    page = pdf.new_page(width=612, height=792)
    page.insert_text((510, 62), f"Page {page_no}", fontsize=11)
    for i, entry in enumerate(["1 5:5 11:6,7", "12 5:5 36:6", "15 5:9", "17 5:11", "19 12:10",
                               "recall 20:7,16 21:3", "Rivera 10:5 12:2"]):
        page.insert_text((60, 90 + i * 20), entry, fontsize=10)


@pytest.fixture
def full_size_pdf(tmp_path):
    import pymupdf as fitz

    pdf = fitz.open()
    full_size_page(pdf, 1, ["IN THE UNITED STATES DISTRICT COURT"])
    for n, (page_no, lines) in enumerate(TESTIMONY.items(), start=2):
        full_size_page(pdf, page_no, lines, ecf=f"Case 1:26-cv-00001 Document 40-2 Filed 03/01/27 Page {n} of 6")
    word_index_page(pdf, 30)
    path = tmp_path / "Lee deposition.pdf"
    pdf.save(path)
    return path


def test_full_size_transcript(full_size_pdf):
    lines = parse_transcript(full_size_pdf)
    pages = sorted({ln.page for ln in lines})
    assert 1 in pages and 10 in pages and 13 in pages
    assert 30 not in pages and 6 not in pages            # the word index; the ECF "Page 6 of 6"
    by_cite = {(ln.page, ln.line): ln for ln in lines}
    assert by_cite[(11, 2)].text == "A Yes. I e-mailed human resources on" and by_cite[(11, 2)].pdf_page == 3
    assert by_cite[(10, 25)].text == ""                    # numbered but blank lines are kept


def test_label_drawn_after_text_and_22_lines(tmp_path):
    import pymupdf as fitz

    pdf = fitz.open()
    for page_no, lines in TESTIMONY.items():
        full_size_page(pdf, page_no, lines, n_lines=22, label_after_text=True)
    path = tmp_path / "v.pdf"
    pdf.save(path)
    lines = parse_transcript(path)
    assert {ln.page for ln in lines} == set(TESTIMONY) and max(ln.line for ln in lines) == 22


def test_condensed_four_up(tmp_path):
    import pymupdf as fitz

    pdf = fitz.open()
    condensed_sheet(pdf, [10, 11, 12, 13], TESTIMONY)
    condensed_sheet(pdf, [14, 15, 16, 17], {14: ["Q Anything else?", "A No."]})
    path = tmp_path / "condensed.pdf"
    pdf.save(path)
    lines = parse_transcript(path)
    assert sorted({ln.page for ln in lines}) == list(range(10, 18))
    by_cite = {(ln.page, ln.line): ln for ln in lines}
    assert by_cite[(12, 2)].text == "A A week later Pat Rivera moved me to" and by_cite[(12, 2)].pdf_page == 1
    assert by_cite[(13, 2)].text == "A I objected in writing the same day."
    assert by_cite[(14, 1)].pdf_page == 2 and len([ln for ln in lines if ln.page == 11]) == 25


def test_not_a_transcript(tmp_path):
    import pymupdf as fitz

    pdf = fitz.open()
    for i in range(4):
        pdf.new_page().insert_text((72, 72), f"Ordinary letter page {i + 1}. Page {i + 1}")
    path = tmp_path / "letter.pdf"
    pdf.save(path)
    assert parse_transcript(path) == []


def test_cites_and_quotes():
    assert cite((45, 3), (45, 3)) == "45:3"
    assert cite((45, 3), (45, 12)) == "45:3-12"
    assert cite((45, 24), (46, 2)) == "45:24-46:2"
    rows = [{"page": 11, "line": 2, "text": "A Yes. I e-mailed human resources on"},
            {"page": 11, "line": 3, "text": "March 3, 2026, about unpaid hours."},
            {"page": 12, "line": 1, "text": "Q What happened after that?"}]
    assert quote_cite(rows, "I e-mailed human resources on March 3, 2026") == "11:2-3"
    assert quote_cite(rows, "unpaid hours. Q What happened") == "11:3-12:1"
    assert quote_cite(rows, "never said this") is None
    assert plain_terms('"night shift" OR overtime -exempt') == ["night", "shift", "overtime"]
    assert mark_terms("<b>complained</b> twice", ["complain"]) == "<b>\x02complained\x03</b> twice"
    assert source_href(7, "11:2-3", transcript=True) == "/documents/7?tp=11#t-11-2"
    assert source_href(7, "p. 4") == "/documents/7?page=4#p-4"


@pytest.fixture
def client(settings, full_size_pdf):
    c = TestClient(create_app(settings), base_url="http://127.0.0.1")
    r = c.post("/matters", data={"short_name": "Rivera v. Fictional", "caption": "Rivera v. Fictional Logistics",
                                 "side": "employee"}, follow_redirects=False)
    c.mid = int(r.headers["location"].rsplit("/", 1)[1])
    c.conn = c.app.state.conn
    m = get_matter(c.conn, c.mid)
    c.root = matter_folder(m, settings.matters_dir)
    c.doc = ingest_file(c.conn, m, c.root, full_size_pdf).id
    return c


def test_ingest_recognizes_transcripts(client):
    row = client.conn.execute("SELECT doc_type FROM documents WHERE id = ?", (client.doc,)).fetchone()
    assert row["doc_type"] == "transcript"
    assert client.conn.execute("SELECT COUNT(DISTINCT page) FROM transcript_lines").fetchone()[0] == 5


def test_search_and_document_page_cite_page_and_line(client):
    hit = search(client.conn, "night shift", client.mid)[0]
    assert hit["page_no"] == 4                                           # the PDF page
    r = client.get("/search", params={"q": "night shift"})
    assert "Tr. 12:3" in r.text and f"/documents/{client.doc}?tp=12&q=night%20shift#t-12-3" in r.text
    assert "pin=12%3A3" in r.text                                        # "+ chronology" carries the cite

    page = client.get(f"/documents/{client.doc}?tp=12&q=night").text
    assert 'id="t-12-3" data-cite="12:3"' in page and "<mark>night</mark>" in page
    assert 'class="page transcript current" id="t-12"' in page
    assert "<title>Lee deposition · Rivera v. Fictional</title>" in page   # nothing else in the title


def test_chronology_links_to_the_line(client):
    from lpc.chronology import save_event

    save_event(client.conn, client.mid, {"description": "Rivera moves Lee to nights", "source_document_id": client.doc,
                                         "pin": "12:2-3"}, [])
    client.conn.commit()
    assert f'href="/documents/{client.doc}?tp=12#t-12-2"' in client.get(f"/matters/{client.mid}/chronology").text


def test_read_as_transcript_for_older_documents(client, tmp_path):
    client.conn.execute("DELETE FROM transcript_lines")
    client.conn.commit()
    page = client.get(f"/documents/{client.doc}").text
    assert "Read as transcript" in page
    r = client.post(f"/documents/{client.doc}/transcript", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Read 5 transcript pages" in r.text
    assert client.conn.execute("SELECT COUNT(*) FROM transcript_lines").fetchone()[0] > 0

    import pymupdf as fitz

    plain = tmp_path / "memo.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Just a memo about the night shift schedule for March.")
    pdf.save(plain)
    m = get_matter(client.conn, client.mid)
    memo = ingest_file(client.conn, m, client.root, plain).id
    r = client.post(f"/documents/{memo}/transcript", headers={"HX-Request": "true"})
    assert r.status_code == 400 and "No numbered transcript pages" in r.text


def test_ai_suggestions_on_transcripts_get_page_line_pins(client, monkeypatch):
    import json

    import httpx

    from lpc.ai.events import suggest_events

    client.conn.execute("UPDATE matters SET ai_permitted = 1 WHERE id = ?", (client.mid,))
    monkeypatch.setattr("lpc.ai.gateway.get_api_key", lambda: "k")
    events = [{"date": "2026-03-03", "description": "Lee e-mailed HR about unpaid hours.", "people": [], "page": 3,
               "quote": "I e-mailed human resources on March 3, 2026", "significance": "key"}]
    payload = {"content": [{"type": "text", "text": json.dumps(events)}], "stop_reason": "end_turn", "usage": {}}
    api = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    suggest_events(client.conn, client.doc, client=api)
    assert client.conn.execute("SELECT pin FROM events").fetchone()[0] == "11:2-3"


def test_real_world_layouts_are_documented():
    # The module docstring records what the parser was checked against; keep it honest if it changes.
    import lpc.transcripts as t

    assert "condensed" in t.__doc__ and "word index" in t.__doc__ and Path(t.__file__).name == "transcripts.py"
