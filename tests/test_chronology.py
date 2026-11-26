import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.chronology import (
    EventError, chronology, format_event_date, next_event_code, parse_event_date, pin_page, save_event,
)
from lpc.ingest import ingest_file
from lpc.matters import add_party_to_matter, create_matter, get_matter, matter_folder

HX = {"HX-Request": "true"}


def test_parse_and_format_partial_dates():
    assert parse_event_date("2026-03-03") == ("2026-03-03", "day")
    assert parse_event_date("3/3/2026") == ("2026-03-03", "day")
    assert parse_event_date("2026-3") == ("2026-03", "month")
    assert parse_event_date("3/2026") == ("2026-03", "month")
    assert parse_event_date("2026") == ("2026", "year")
    assert parse_event_date("2026-03", approximate=True) == ("2026-03", "approximate")
    assert parse_event_date("") == (None, "unknown")
    for bad in ("2026-02-30", "13/1/2026", "last spring"):
        with pytest.raises(EventError):
            parse_event_date(bad)
    assert format_event_date("2026-03-03", "day") == "Mar 3, 2026"
    assert format_event_date("2026-03", "approximate") == "c. Mar 2026"
    assert format_event_date(None) == "Undated"
    assert pin_page("p. 17") == 17 and pin_page("45:3-12") == 45 and pin_page("") is None


@pytest.fixture
def matter(conn, settings):
    mid = create_matter(conn, {"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee"},
                        settings.matters_dir)
    add_party_to_matter(conn, mid, "Jane Doe", "client")
    add_party_to_matter(conn, mid, "Pat Manager", "decision_maker")
    conn.commit()
    people = {r["name"]: r["id"] for r in conn.execute(
        "SELECT mp.id, p.name FROM matter_parties mp JOIN parties p ON p.id = mp.party_id")}
    return mid, people


def test_events_sort_by_partial_date_and_filter(conn, matter):
    mid, people = matter
    add = lambda date, prec, text, who=(), **kw: save_event(  # noqa: E731
        conn, mid, {"event_date": date, "date_precision": prec, "description": text, **kw},
        [people[w] for w in who])
    add("2026-03-03", "day", "Doe complains to HR about overtime", ["Jane Doe"], significance="key",
        issue_tags="retaliation, overtime")
    add(None, "unknown", "Undated note in file")
    add("2026", "year", "Doe hired", ["Jane Doe"], issue_tags="background")
    add("2026-03-10", "day", "Manager moves Doe to nights", ["Jane Doe", "Pat Manager"], significance="key",
        disputed=1, issue_tags="Retaliation")
    conn.commit()
    codes = [e["event_code"] for e in chronology(conn, mid)]
    assert codes == ["E-003", "E-001", "E-004", "E-002"]               # 2026, 3/3, 3/10, undated
    assert next_event_code(conn, mid) == "E-005"
    assert [e["event_code"] for e in chronology(conn, mid, person=people["Pat Manager"])] == ["E-004"]
    assert [e["event_code"] for e in chronology(conn, mid, issue="retaliation")] == ["E-001", "E-004"]
    assert [e["event_code"] for e in chronology(conn, mid, disputed_only=True)] == ["E-004"]
    assert chronology(conn, mid)[2]["people"] in ("Jane Doe, Pat Manager", "Pat Manager, Jane Doe")
    tags = conn.execute("SELECT issue_tags FROM events WHERE event_code = 'E-001'").fetchone()[0]
    assert tags == "retaliation, overtime"


def test_events_refuse_other_matters_documents(conn, matter, settings, tmp_path):
    mid, _ = matter
    other = create_matter(conn, {"short_name": "Other", "caption": "Other", "side": "employer"}, settings.matters_dir)
    m = get_matter(conn, other)
    src = tmp_path / "x.txt"
    src.write_text("x")
    doc = ingest_file(conn, m, matter_folder(m, settings.matters_dir), src).id
    with pytest.raises(EventError, match="not in this matter"):
        save_event(conn, mid, {"description": "x", "source_document_id": doc}, [])
    with pytest.raises(EventError, match="Describe"):
        save_event(conn, mid, {"description": " "}, [])


@pytest.fixture
def client(settings):
    c = TestClient(create_app(settings), base_url="http://127.0.0.1")
    r = c.post("/matters", data={"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee",
                                 "client_name": "Jane Doe", "client_kind": "person"}, follow_redirects=False)
    c.mid = int(r.headers["location"].rsplit("/", 1)[1])
    c.conn = c.app.state.conn
    return c


def test_add_edit_delete_through_the_page(client, settings, tmp_path):
    m = get_matter(client.conn, client.mid)
    src = tmp_path / "email.txt"
    src.write_text("I am moving Doe to nights effective Monday.")
    doc = ingest_file(client.conn, m, matter_folder(m, settings.matters_dir), src).id
    client.conn.execute("UPDATE documents SET doc_date = '2026-03-10' WHERE id = ?", (doc,))
    jane = client.conn.execute("SELECT id FROM matter_parties").fetchone()[0]

    # "+ chronology" from a page: document, date, pin and selected text are filled in
    page = client.get(f"/matters/{client.mid}/chronology?doc={doc}&pin=p.%201&text=moving+Doe+to+nights").text
    assert f'<option value="{doc}" selected>' in page and 'value="2026-03-10"' in page
    assert 'value="p. 1"' in page and "moving Doe to nights</textarea>" in page and "<details class=\"card\" id=\"add\" open" in page

    r = client.post(f"/matters/{client.mid}/events", data={
        "event_date": "3/10/2026", "description": "Manager moves Doe to nights", "significance": "key",
        "people": str(jane), "source_document_id": str(doc), "pin": "p. 1", "issue_tags": "retaliation"},
        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/chronology#E-001")
    page = client.get(f"/matters/{client.mid}/chronology").text
    assert "Mar 10, 2026" in page and "Manager moves Doe to nights" in page and "Jane Doe" in page
    assert f'href="/documents/{doc}?page=1#p-1"' in page

    event_id = client.conn.execute("SELECT id FROM events").fetchone()[0]
    assert "Edit E-001" in client.get(f"/events/{event_id}/edit").text
    client.post(f"/events/{event_id}/edit", data={"event_date": "2026-03", "approximate": "1",
                                                   "description": "Doe moved to nights", "significance": "key"})
    row = client.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert (row["event_date"], row["date_precision"], row["source_document_id"]) == ("2026-03", "approximate", None)
    assert client.conn.execute("SELECT COUNT(*) FROM event_people").fetchone()[0] == 0   # people unticked
    assert "c. Mar 2026" in client.get(f"/matters/{client.mid}/chronology").text

    r = client.post(f"/matters/{client.mid}/events", data={"event_date": "spring", "description": "x"})
    assert r.status_code == 400 and "Could not read the date" in r.text

    client.post(f"/events/{event_id}/delete")
    assert client.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    log = [r[0] for r in client.conn.execute("SELECT summary FROM changelog WHERE entity = 'events' ORDER BY id")]
    assert log == ["Added event E-001: Manager moves Doe to nights", "Edited event E-001", "Deleted event E-001"]


def test_add_to_chronology_links(client, settings, tmp_path):
    m = get_matter(client.conn, client.mid)
    src = tmp_path / "memo.txt"
    src.write_text("Doe was moved to nights.")
    doc = ingest_file(client.conn, m, matter_folder(m, settings.matters_dir), src).id
    hit = client.get("/search", params={"q": "nights"}).text
    assert f"/matters/{client.mid}/chronology?doc={doc}&pin=p.%201#add" in hit
    page = client.get(f"/documents/{doc}").text
    assert 'class="small add-event"' in page and f"/matters/{client.mid}/chronology?doc={doc}#add" in page
    assert f'href="/matters/{client.mid}/chronology"' in client.get(f"/matters/{client.mid}").text
