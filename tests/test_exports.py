import csv
import io

import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.chronology import save_event
from lpc.exports import export_name
from lpc.ingest import ingest_file
from lpc.matters import get_matter, matter_folder

HX = {"HX-Request": "true"}


@pytest.fixture
def client(settings, tmp_path, monkeypatch):
    import lpc.app

    opened = []
    monkeypatch.setattr(lpc.app, "open_in_os", lambda path, reveal=False: opened.append(path))
    c = TestClient(create_app(settings), base_url="http://127.0.0.1")
    r = c.post("/matters", data={"short_name": "Doe v. Acme", "caption": "Jane Doe v. Acme Logistics, Inc.",
                                 "side": "employee", "client_name": "Jane Doe", "client_kind": "person"},
               follow_redirects=False)
    c.mid = int(r.headers["location"].rsplit("/", 1)[1])
    c.post(f"/matters/{c.mid}/parties", data={"name": "Pat Manager", "role": "decision_maker", "title": "Shift lead"})
    conn = c.app.state.conn
    people = {n: i for i, n in conn.execute(
        "SELECT mp.id, p.name FROM matter_parties mp JOIN parties p ON p.id = mp.party_id")}
    m = get_matter(conn, c.mid)
    src = tmp_path / "schedule email.txt"
    src.write_text("Moving Doe to nights.")
    doc = ingest_file(conn, m, matter_folder(m, settings.matters_dir), src).id
    conn.execute("UPDATE documents SET bates_begin = 'ACME000101' WHERE id = ?", (doc,))
    save_event(conn, c.mid, {"event_date": "2026-03-03", "date_precision": "day", "significance": "key",
                             "description": "Doe complains to HR about unpaid overtime", "issue_tags": "retaliation"},
               [people["Jane Doe"]])
    save_event(conn, c.mid, {"event_date": "2026-03-10", "date_precision": "day", "significance": "key", "disputed": 1,
                             "description": "Manager moves Doe to nights", "source_document_id": doc, "pin": "p. 1",
                             "issue_tags": "retaliation", "notes": "Manager says it was routine"},
               [people["Jane Doe"], people["Pat Manager"]])
    save_event(conn, c.mid, {"event_date": "2025", "date_precision": "year", "description": "Doe hired"},
               [people["Jane Doe"]])
    conn.commit()
    c.conn, c.people, c.opened = conn, people, opened
    c.work = settings.matters_dir / "Doe v. Acme" / "20 Work Product"
    return c


def test_export_name():
    import datetime as dt

    assert export_name("Doe v. Acme", "Chronology", ".docx", dt.date(2026, 11, 26)) == "Doe v. Acme - 2026.11.26 - Chronology.docx"
    assert export_name("Roe: v/ Acme", "Depo outline - Pat", ".docx", dt.date(2026, 1, 2)).startswith("Roe- v- Acme - 2026.01.02")


def test_word_chronology_table(client):
    from docx import Document

    r = client.post(f"/matters/{client.mid}/chronology/export", data={"format": "docx"}, headers=HX)
    assert r.status_code == 200 and r.headers["HX-Retarget"] == "#flash" and "Saved 3 events to" in r.text
    [path] = client.work.glob("*.docx")
    assert path.name.startswith("Doe v. Acme - ") and path.name.endswith(" - Chronology.docx")
    assert client.opened == [path]
    doc = Document(str(path))
    rows = [[c.text for c in row.cells] for row in doc.tables[0].rows]
    assert rows[0] == ["Code", "Date", "What happened", "People", "Source"]
    assert [r[1] for r in rows[1:]] == ["2025", "Mar 3, 2026", "Mar 10, 2026"]
    assert "Manager moves Doe to nights  [disputed]" in rows[3][2] and "routine" in rows[3][2]
    assert rows[3][4] == "schedule email, p. 1 (ACME000101)"
    assert "work product" in doc.sections[0].header.paragraphs[0].text
    assert "Exported Chronology to 20 Work Product/" in client.conn.execute(
        "SELECT summary FROM changelog ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_exports_follow_the_filters(client):
    from openpyxl import load_workbook

    r = client.post(f"/matters/{client.mid}/chronology/export",
                    data={"format": "xlsx", "person": str(client.people["Pat Manager"])}, headers=HX)
    assert "Saved 1 event to" in r.text
    ws = load_workbook(next(client.work.glob("*.xlsx"))).active
    assert ws["A1"].value == "Code" and ws.max_row == 2 and ws["D2"].value == "Manager moves Doe to nights"
    assert ws.freeze_panes == "A2"

    client.post(f"/matters/{client.mid}/chronology/export", data={"format": "csv", "issue": "retaliation"})
    raw = next(client.work.glob("*.csv")).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0][:4] == ["Code", "Date", "Precision", "What happened"] and len(rows) == 3

    r = client.post(f"/matters/{client.mid}/chronology/export", data={"format": "csv", "issue": "nothing"}, headers=HX)
    assert r.status_code == 400 and "no events" in r.text
    r = client.post(f"/matters/{client.mid}/chronology/export", data={"format": "pdf"}, headers=HX)
    assert r.status_code == 400


def test_witness_outline(client):
    from docx import Document

    r = client.post(f"/matters/{client.mid}/chronology/witness", data={"witness": str(client.people["Pat Manager"])},
                    headers=HX)
    assert "Saved an outline with 1 event to" in r.text
    [path] = client.work.glob("*Depo outline - Pat Manager.docx")
    text = [p.text for p in Document(str(path)).paragraphs]
    assert text[0] == "Deposition outline: Pat Manager"
    assert "Decision maker, Shift lead" in text[1]
    assert "E-002  Mar 10, 2026" in text and "Manager moves Doe to nights  [disputed]" in text
    assert "Source: schedule email, p. 1 (ACME000101)" in text
    assert "Doe complains to HR about unpaid overtime" not in text              # not Pat's event
    assert text[text.index("Exhibits to use") + 1] == "schedule email (ACME000101)"

    r = client.post(f"/matters/{client.mid}/chronology/witness", data={"witness": ""}, headers=HX)
    assert r.status_code == 400 and "Choose the person" in r.text


def test_chronology_page_has_export_controls(client):
    page = client.get(f"/matters/{client.mid}/chronology").text
    assert 'hx-include="#chrono-filters"' in page and "Witness outline" in page
