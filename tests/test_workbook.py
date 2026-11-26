import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.chronology import chronology, save_event
from lpc.exports import chronology_xlsx
from lpc.ingest import ingest_file
from lpc.matters import add_party_to_matter, create_matter, get_matter, matter_folder
from lpc.workbook import TEMPLATE_COLUMNS, import_rows, key_events_template, read_workbook


def save_xlsx(path, sheets: dict):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


# A workbook the way people actually keep them: a title above the header, their own column names,
# Excel date cells mixed with typed dates, and a "Key Events" tab that is not the first sheet.
MESSY = {
    "Notes": [["scratch"]],
    "Key Events": [
        ["Rivera v. Fictional Logistics - Key Events"],
        [],
        ["Event ID", "When", "What happened", "Key Event", "Contested", "Witnesses", "Tags", "Bates", "Cite", "Comments"],
        [1, dt.datetime(2025, 1, 6), "Rivera hired as warehouse associate", "", "", "Ana Rivera", "background",
         "", "", ""],
        [2, "3/3/2026", "Rivera e-mails HR about unpaid overtime", "Y", "", "Ana Rivera; Sam Ortiz",
         "overtime, retaliation", "FL000101", "p. 1", ""],
        [3, "2026-03", "Rivera moved to nights", "Y", "Y", "Pat Lane", "retaliation", "Shift roster", 4,
         "Lane says routine"],
        [4, "spring 2024", "Earlier attendance warning", "", "", "", "", "Warning letter", "", ""],
        [5, None, None, None, None, None, None, None, None, None],
        [6, "", "", "", "", "Nobody", "", "", "", "row with no event text"],
    ],
}


@pytest.fixture
def matter(conn, settings, tmp_path):
    mid = create_matter(conn, {"short_name": "Rivera v. Fictional", "caption": "Rivera v. Fictional Logistics",
                               "side": "employee"}, settings.matters_dir)
    add_party_to_matter(conn, mid, "Ana Rivera", "client")
    m = get_matter(conn, mid)
    root = matter_folder(m, settings.matters_dir)
    for name, bates in (("HR complaint email", "FL000101"), ("Shift roster", None)):
        src = tmp_path / f"{name}.txt"
        src.write_text(name)
        doc = ingest_file(conn, m, root, src).id
        conn.execute("UPDATE documents SET bates_begin = ? WHERE id = ?", (bates, doc))
    conn.commit()
    return mid


def test_template(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "template.xlsx"
    path.write_bytes(key_events_template())
    wb = load_workbook(path)
    assert wb.sheetnames == ["Key Events", "How to fill it in"]
    assert tuple(c.value for c in wb["Key Events"][1]) == TEMPLATE_COLUMNS
    assert wb["Key Events"]["C2"].value.startswith("Example:")
    preview = read_workbook(path)
    assert preview.rows == [] and preview.skipped_examples == 1      # the example row is never imported


def test_read_messy_workbook(tmp_path):
    preview = read_workbook(save_xlsx(tmp_path / "events.xlsx", MESSY))
    assert preview.sheet == "Key Events"
    assert preview.mapped == {"When": "date", "What happened": "description", "Key Event": "significance",
                              "Contested": "disputed", "Witnesses": "people", "Tags": "issues", "Bates": "bates",
                              "Cite": "pin", "Comments": "notes"}
    assert preview.ignored == ["Event ID"]
    rows = {r.number: r for r in preview.rows}
    assert sorted(rows) == [4, 5, 6, 7, 9]                           # blank row 8 dropped
    assert rows[4].data["event_date"] == "2025-01-06" and rows[4].data["significance"] == "supporting"
    assert rows[5].data["event_date"] == "2026-03-03" and rows[5].data["significance"] == "key"
    assert rows[5].people == ["Ana Rivera", "Sam Ortiz"] and rows[5].source_text == "FL000101"
    assert rows[6].data["date_precision"] == "month" and rows[6].data["disputed"] == 1 and rows[6].data["pin"] == "p. 4"
    assert rows[7].data["event_date"] is None and "Date in workbook: spring 2024" in rows[7].data["notes"]
    assert "no Event text" in rows[9].problems[0]


def test_import_links_sources_and_people_and_skips_repeats(conn, matter, tmp_path):
    path = save_xlsx(tmp_path / "events.xlsx", MESSY)
    counts = import_rows(conn, matter, read_workbook(path), add_people=True, file_name="events.xlsx")
    assert counts == {"added": 4, "duplicates": 0, "skipped": 1, "people_added": 2}
    events = {e["description"]: e for e in chronology(conn, matter)}
    complaint = events["Rivera e-mails HR about unpaid overtime"]
    assert complaint["source_title"] == "HR complaint email"                 # matched by Bates
    assert complaint["people"] in ("Ana Rivera, Sam Ortiz", "Sam Ortiz, Ana Rivera")
    assert complaint["origin"] == "import" and complaint["status"] == "accepted"
    assert events["Rivera moved to nights"]["source_title"] == "Shift roster"   # matched by title
    warning = events["Earlier attendance warning"]
    assert "Source in workbook: Warning letter" in warning["notes"] and "spring 2024" in warning["notes"]
    roles = {r[0]: r[1] for r in conn.execute(
        "SELECT p.name, mp.role FROM matter_parties mp JOIN parties p ON p.id = mp.party_id")}
    assert roles["Pat Lane"] == "witness" and roles["Ana Rivera"] == "client"

    again = import_rows(conn, matter, read_workbook(path), add_people=True, file_name="events.xlsx")
    assert again["added"] == 0 and again["duplicates"] == 4


def test_names_can_go_to_notes_instead(conn, matter, tmp_path):
    import_rows(conn, matter, read_workbook(save_xlsx(tmp_path / "e.xlsx", MESSY)), add_people=False, file_name="e")
    nights = next(e for e in chronology(conn, matter) if e["description"] == "Rivera moved to nights")
    assert nights["people"] is None and "Also named: Pat Lane" in nights["notes"]


def test_csv_import(conn, matter, tmp_path):
    path = tmp_path / "events.csv"
    path.write_text("Date,Event,People,Source,Pin\n2026-03-03,Complaint to HR,Ana Rivera,HR complaint email,p. 1\n",
                    encoding="utf-8-sig")
    counts = import_rows(conn, matter, read_workbook(path), add_people=True, file_name="events.csv")
    assert counts["added"] == 1
    assert chronology(conn, matter)[0]["source_title"] == "HR complaint email"


def test_export_then_import_into_another_matter(conn, matter, settings, tmp_path):
    jane = conn.execute("SELECT id FROM matter_parties WHERE matter_id = ?", (matter,)).fetchone()[0]
    save_event(conn, matter, {"event_date": "2026-03", "date_precision": "approximate", "significance": "key",
                              "disputed": 1, "description": "Moved to nights", "issue_tags": "retaliation"}, [jane])
    conn.commit()
    m = get_matter(conn, matter)
    path = tmp_path / "export.xlsx"
    path.write_bytes(chronology_xlsx(m, chronology(conn, matter)))
    other = create_matter(conn, {"short_name": "Second", "caption": "Second", "side": "employee"}, settings.matters_dir)
    import_rows(conn, other, read_workbook(path), add_people=True, file_name="export.xlsx")
    e = chronology(conn, other)[0]
    assert (e["event_date"], e["date_precision"], e["significance"], e["disputed"], e["issue_tags"], e["people"]) == (
        "2026-03", "approximate", "key", 1, "retaliation", "Ana Rivera")


def test_unreadable_files(tmp_path):
    from lpc.workbook import WorkbookError

    bad = tmp_path / "x.xlsx"
    bad.write_bytes(b"not a workbook")
    with pytest.raises(WorkbookError, match="Could not open"):
        read_workbook(bad)
    no_header = save_xlsx(tmp_path / "y.xlsx", {"Sheet": [["a", "b"], [1, 2]]})
    with pytest.raises(WorkbookError, match="header row"):
        read_workbook(no_header)
    with pytest.raises(WorkbookError, match=".xlsx"):
        read_workbook(tmp_path / "z.pdf")


def test_import_pages(settings, matter, tmp_path, monkeypatch):
    import lpc.app

    monkeypatch.setattr(lpc.app, "open_in_os", lambda path, reveal=False: None)
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    data = save_xlsx(tmp_path / "events.xlsx", MESSY).read_bytes()

    r = client.post(f"/matters/{matter}/chronology/import", files={"file": ("Key Events.xlsx", data)},
                    data={"add_people": "1"})
    assert r.status_code == 200 and "Nothing has been saved yet" in r.text and "Import 4 events" in r.text
    assert "<strong>What happened</strong> &rarr; Event" in r.text and "Event ID</strong>: not imported" in r.text
    assert "Warning letter" in r.text and "Pat Lane" in r.text
    assert "witnesses: Pat Lane, Sam Ortiz" in r.text                        # not "Nobody": that row is skipped
    assert chronology(client.app.state.conn, matter) == []                     # preview saves nothing
    token = r.text.split('name="token" value="')[1].split('"')[0]

    r = client.post(f"/matters/{matter}/chronology/import/confirm",
                    data={"token": token, "file_name": "Key Events.xlsx", "add_people": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "imported=4" in r.headers["location"]
    assert "Imported 4 events" in client.get(r.headers["location"]).text
    assert not (settings.data_dir / "staging" / token).exists()

    r = client.post(f"/matters/{matter}/chronology/import/confirm", data={"token": token},
                    headers={"HX-Request": "true"})
    assert r.status_code == 400 and "expired" in r.text
    r = client.post(f"/matters/{matter}/chronology/import/confirm", data={"token": "../../lpc.sqlite3"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 400
    r = client.post(f"/matters/{matter}/chronology/import", files={"file": ("notes.pdf", b"%PDF")})
    assert r.status_code == 400 and ".xlsx" in r.text

    r = client.post(f"/matters/{matter}/chronology/template", headers={"HX-Request": "true"})
    assert "Saved a blank Key Events workbook" in r.text
    assert list((settings.matters_dir / "Rivera v. Fictional" / "20 Work Product").glob("*Key Events template.xlsx"))
    assert "Import key events from a workbook" in client.get(f"/matters/{matter}/chronology").text
