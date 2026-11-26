import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lpc.ai.events import chunks, normalize, parse_reply, suggest_events
from lpc.ai.gateway import GatewayBlocked
from lpc.app import create_app
from lpc.chronology import chronology
from lpc.ingest import ingest_file
from lpc.jobs import run_pending
from lpc.matters import add_party_to_matter, create_matter, get_matter, matter_folder

PAGES = [
    "Offer letter. Jane Doe will start as a warehouse associate on January 6, 2025.",
    "On March 3, 2026 Doe e-mailed HR: I have not been paid for the overtime I worked in February.",
    "Effective March 10, 2026, Pat Manager reassigned Doe to the night shift. Doe objected in writ-\ning.",
]


def reply(events):
    """A Messages API response whose text is the given events as JSON."""
    return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(events)}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 900, "output_tokens": 300}}


def fake_api(payloads, seen=None):
    queue = list(payloads)

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


GOOD = [
    {"date": "2026-03-03", "approximate": False, "description": "Doe complained to HR about unpaid February overtime.",
     "people": ["Jane Doe", "HR"], "page": 2, "quote": "I have not been paid for the overtime I worked in February",
     "significance": "key"},
    {"date": "2026-03-10", "description": "Pat Manager moved Doe to the night shift.", "people": ["Pat Manager"],
     "page": 2,                                                   # wrong page: the quote is on page 3
     "quote": "Pat Manager reassigned Doe to the night shift", "significance": "key"},
    {"date": "2026-03", "description": "Doe objected in writing.", "people": ["Jane Doe"], "page": 3,
     "quote": "Doe objected in writing"},                         # hyphenated across a line in the PDF text
    {"date": "2026-04-01", "description": "Doe was fired.", "people": ["Doe"], "page": 3,
     "quote": "Doe was terminated for attendance"},               # not in the document: dropped
    {"date": "soon", "description": "Doe started work.", "people": ["Jane Doe"], "page": 1,
     "quote": "will start as a warehouse associate", "significance": "background"},
]


@pytest.fixture
def doc(conn, settings, tmp_path, monkeypatch):
    import pymupdf as fitz

    monkeypatch.setattr("lpc.ai.gateway.get_api_key", lambda: "test-key")
    mid = create_matter(conn, {"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee",
                               "ai_permitted": 1}, settings.matters_dir)
    add_party_to_matter(conn, mid, "Jane Doe", "client")
    add_party_to_matter(conn, mid, "Pat Manager", "decision_maker")
    add_party_to_matter(conn, mid, "John Doe", "witness")
    m = get_matter(conn, mid)
    path = tmp_path / "doe file.pdf"
    pdf = fitz.open()
    for text in PAGES:
        pdf.new_page().insert_textbox(fitz.Rect(72, 72, 540, 760), text)
    pdf.save(path)
    doc_id = ingest_file(conn, m, matter_folder(m, settings.matters_dir), path).id
    conn.commit()
    return mid, doc_id


def test_helpers():
    assert normalize("“Smart” quotes — and  em dash, re-\nhyphen") == '"smart" quotes - and em dash, rehyphen'
    assert parse_reply('Here you go:\n```json\n[{"a": 1}, 2]\n```') == [{"a": 1}]
    with pytest.raises(ValueError):
        parse_reply("Sorry, I cannot help with that.")
    pages = [(1, "a" * 40), (2, "b" * 40), (3, "c" * 100)]
    assert [[n for n, _ in c] for c in chunks(pages, limit=90)] == [[1, 2], [3]]


def test_suggestions_are_verified_and_stored_as_proposed(conn, doc):
    mid, doc_id = doc
    seen = []
    summary, problems = suggest_events(conn, doc_id, client=fake_api([reply(GOOD)], seen))
    assert summary == "4 events proposed for review, 1 dropped because the quote was not found on the page"
    assert problems == []
    rows = {r["description"]: r for r in chronology(conn, mid, status="proposed")}
    assert chronology(conn, mid) == []                                    # nothing in the chronology yet
    complaint = rows["Doe complained to HR about unpaid February overtime."]
    assert (complaint["event_date"], complaint["pin"], complaint["status"], complaint["origin"]) == (
        "2026-03-03", "p. 2", "proposed", "ai")
    assert complaint["people"] == "Jane Doe" and complaint["notes"] == "Also named: HR"
    assert rows["Pat Manager moved Doe to the night shift."]["pin"] == "p. 3"             # page corrected
    assert rows["Doe objected in writing."]["date_precision"] == "month"
    assert rows["Doe started work."]["event_date"] is None                                 # unreadable date
    assert {r["event_code"] for r in rows.values()} == {"P-001", "P-002", "P-003", "P-004"}

    body = seen[0]
    assert body["model"] == "claude-opus-5-5" and body["output_config"] == {"effort": "medium"}
    assert "[Page 2]" in body["messages"][0]["content"] and "Pat Manager" in body["messages"][0]["content"]
    call = conn.execute("SELECT * FROM ai_calls").fetchone()
    assert call["purpose"] == "chronology" and call["document_ids"] == f"[{doc_id}]" and call["status"] == "ok"
    assert complaint["ai_call_id"] == call["id"]

    # asking again does not duplicate what was already suggested
    summary, _ = suggest_events(conn, doc_id, client=fake_api([reply(GOOD)]))
    assert summary.startswith("0 events proposed") and "4 already suggested" in summary


def test_ambiguous_names_are_not_guessed(conn, doc):
    mid, doc_id = doc
    events = [dict(GOOD[0], people=["Doe"])]          # Jane Doe and John Doe are both parties
    suggest_events(conn, doc_id, client=fake_api([reply(events)]))
    row = chronology(conn, mid, status="proposed")[0]
    assert row["people"] is None and row["notes"] == "Also named: Doe"


def test_unreadable_reply_is_reported(conn, doc):
    _, doc_id = doc
    bad = {"content": [{"type": "text", "text": "I could not find any events."}], "stop_reason": "end_turn",
           "usage": {}}
    summary, problems = suggest_events(conn, doc_id, client=fake_api([bad]))
    assert summary.startswith("0 events proposed") and "could not read the model's reply" in problems[0]


def test_no_consent_means_nothing_is_sent(conn, doc):
    mid, doc_id = doc
    conn.execute("UPDATE matters SET ai_permitted = 0 WHERE id = ?", (mid,))
    seen = []
    with pytest.raises(GatewayBlocked, match="consented"):
        suggest_events(conn, doc_id, client=fake_api([reply(GOOD)], seen))
    assert seen == [] and conn.execute("SELECT status FROM ai_calls").fetchone()[0] == "blocked"


def test_review_accept_reject_and_job(conn, doc, settings, monkeypatch):
    mid, doc_id = doc
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    monkeypatch.setattr("lpc.ai.gateway._http_client", lambda: fake_api([reply(GOOD)]))

    r = client.post(f"/documents/{doc_id}/suggest-events", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Suggestions will appear" in r.text
    run_pending(client.app.state.conn, settings.matters_dir)
    panel = client.get(f"/matters/{mid}/jobs").text
    assert "AI event suggestions from doe file" in panel and "4 events proposed for review" in panel

    page = client.get(f"/matters/{mid}/chronology").text
    assert "Suggested by AI: 4 to review" in page and "I have not been paid for the overtime" in page
    app_conn = client.app.state.conn
    ids = {r["description"]: r["id"] for r in chronology(app_conn, mid, status="proposed")}
    r = client.post(f"/events/{ids['Pat Manager moved Doe to the night shift.']}/accept", follow_redirects=False)
    assert r.headers["location"].endswith("#E-001")
    client.post(f"/events/{ids['Doe started work.']}/reject")
    assert [e["event_code"] for e in chronology(app_conn, mid)] == ["E-001"]
    assert len(chronology(app_conn, mid, status="proposed")) == 2
    assert "Suggested by AI: 2 to review" in client.get(f"/matters/{mid}/chronology").text
    r = client.post(f"/events/{ids['Pat Manager moved Doe to the night shift.']}/reject", headers={"HX-Request": "true"})
    assert r.status_code == 400 and "already in the chronology" not in r.text and "edit or delete" in r.text
    log = [r[0] for r in app_conn.execute("SELECT summary FROM changelog WHERE entity = 'events' ORDER BY id")]
    assert log[0] == "AI proposed 4 events from doe file" and log[1].startswith("Accepted AI-suggested event E-001 (was P-")


def test_suggest_refused_without_consent_or_text(conn, doc, settings):
    mid, doc_id = doc
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    conn.execute("UPDATE matters SET ai_permitted = 0 WHERE id = ?", (mid,))
    conn.commit()
    r = client.post(f"/documents/{doc_id}/suggest-events", headers={"HX-Request": "true"})
    assert r.status_code == 400 and "AI is off" in r.text
    assert "disabled title=\"AI is off" in client.get(f"/documents/{doc_id}").text
