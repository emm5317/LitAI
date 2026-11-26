import datetime as dt

import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.discovery import DiscoveryError, limits_summary, save_set, set_status, to_chase
from lpc.matters import add_party_to_matter, create_matter


@pytest.fixture
def matter(conn, settings):
    mid = create_matter(conn, {"short_name": "Rivera v. Fictional", "caption": "Rivera v. Fictional Logistics",
                               "side": "employee", "rules_profile": "ndil", "status": "active"}, settings.matters_dir)
    add_party_to_matter(conn, mid, "Fictional Logistics, Inc.", "adverse_party", kind="organization")
    conn.commit()
    party = conn.execute("SELECT id FROM matter_parties WHERE matter_id = ?", (mid,)).fetchone()[0]
    return mid, party


def deadline(conn, set_id):
    return conn.execute("SELECT d.* FROM deadlines d JOIN discovery_sets s ON s.deadline_id = d.id WHERE s.id = ?",
                        (set_id,)).fetchone()


def received_rogs(party, **over):
    data = {"direction": "received", "set_type": "interrogatories", "set_label": "Defendant's First Interrogatories",
            "party_matter_party_id": party, "served_on": "2026-11-09", "service_method": "electronic",
            "request_count": 18}
    data.update(over)
    return data


def test_received_set_proposes_a_hard_response_deadline(conn, matter):
    mid, party = matter
    set_id, note = save_set(conn, mid, received_rogs(party))
    d = deadline(conn, set_id)
    assert note is None
    assert (d["due_on"], d["status"], d["hard"], d["rule_id"]) == ("2026-12-09", "proposed", 1, "frcp.interrogatory_responses")
    assert d["title"] == "Respond to Defendant's First Interrogatories (Fictional Logistics, Inc.)"
    assert conn.execute("SELECT response_due_on FROM discovery_sets").fetchone()[0] == "2026-12-09"

    # still proposed: an edit moves it (mail service adds 3 days, FRCP 6(d))
    save_set(conn, mid, received_rogs(party, service_method="mail"), set_id)
    moved = deadline(conn, set_id)
    assert moved["id"] == d["id"] and moved["due_on"] == "2026-12-14"   # Dec 12 is a Saturday -> Mon Dec 14

    # confirmed: never changed here, but the user is told
    conn.execute("UPDATE deadlines SET status = 'confirmed' WHERE id = ?", (d["id"],))
    _, note = save_set(conn, mid, received_rogs(party, served_on="2026-11-16"), set_id)
    assert deadline(conn, set_id)["due_on"] == "2026-12-14" and "already confirmed" in note


def test_served_set_gets_a_soft_reminder_and_answering_closes_it(conn, matter):
    mid, party = matter
    set_id, _ = save_set(conn, mid, {"direction": "served", "set_type": "rfp", "set_label": "Plaintiff's First RFPs",
                                     "party_matter_party_id": party, "served_on": "2026-11-02",
                                     "service_method": "electronic"})
    d = deadline(conn, set_id)
    assert d["hard"] == 0 and d["title"] == "Responses due from Fictional Logistics, Inc. to our Plaintiff's First RFPs"
    set_status(conn, set_id, "responded")
    row = conn.execute("SELECT * FROM discovery_sets WHERE id = ?", (set_id,)).fetchone()
    assert row["status"] == "responded" and row["responded_on"] == dt.date.today().isoformat()
    assert deadline(conn, set_id)["status"] == "done"
    log = [r[0] for r in conn.execute("SELECT summary FROM changelog WHERE entity = 'discovery_sets' ORDER BY id")]
    assert log[-1] == "Discovery Plaintiff's First RFPs: Open -> Responded"


def test_no_rule_means_a_typed_date(conn, matter, settings):
    mid, party = matter
    set_id, note = save_set(conn, mid, {"direction": "received", "set_type": "subpoena", "served_on": "2026-11-10"})
    assert deadline(conn, set_id) is None and "enter the response date" in note
    save_set(conn, mid, {"direction": "received", "set_type": "subpoena", "served_on": "2026-11-10",
                         "response_due_on": "2026-11-24"}, set_id)
    d = deadline(conn, set_id)
    assert (d["due_on"], d["status"], d["source"]) == ("2026-11-24", "confirmed", "manual")

    aaa = create_matter(conn, {"short_name": "Chen", "caption": "Chen v. Northwind", "side": "employer",
                               "rules_profile": "aaa_employment"}, settings.matters_dir)
    set_id, note = save_set(conn, aaa, {"direction": "received", "set_type": "rfp", "served_on": "2026-11-10"})
    assert deadline(conn, set_id) is None and note                      # the arbitrator sets these


def test_drafting_sets_have_no_deadline(conn, matter):
    mid, party = matter
    set_id, _ = save_set(conn, mid, {"direction": "served", "set_type": "rfa", "status": "drafting",
                                     "served_on": "2026-11-10"})
    assert deadline(conn, set_id) is None


def test_limits(conn, matter, settings):
    mid, party = matter
    save_set(conn, mid, {"direction": "served", "set_type": "interrogatories", "party_matter_party_id": party,
                         "request_count": 15, "status": "open"})
    save_set(conn, mid, {"direction": "served", "set_type": "interrogatories", "party_matter_party_id": party,
                         "request_count": 12, "status": "open"})
    save_set(conn, mid, {"direction": "served", "set_type": "rfp", "party_matter_party_id": party, "request_count": 80})
    [rogs] = limits_summary(conn, mid, "ndil")                          # no FRCP limit on RFPs
    assert (rogs["used"], rogs["limit"], rogs["over"], rogs["cite"]) == (27, 25, True, "Fed. R. Civ. P. 33(a)(1)")
    assert rogs["verified"] is False and rogs["party"] == "Fictional Logistics, Inc."

    cook = create_matter(conn, {"short_name": "Cook case", "caption": "C", "side": "employee",
                                "rules_profile": "cook"}, settings.matters_dir)
    save_set(conn, cook, {"direction": "received", "set_type": "rfa", "request_count": 31})
    [rfa] = limits_summary(conn, cook, "cook")
    assert rfa["limit"] == 30 and rfa["over"] and rfa["cite"] == "Ill. S. Ct. R. 216(f)"


def test_chase_list(conn, matter, settings):
    mid, party = matter
    overdue, _ = save_set(conn, mid, {"direction": "served", "set_type": "rfp", "set_label": "Late RFPs",
                                      "served_on": "2026-09-01", "service_method": "electronic"})
    save_set(conn, mid, {"direction": "served", "set_type": "rfa", "set_label": "Fine RFAs",
                         "served_on": "2026-11-20", "service_method": "electronic"})
    stuck, _ = save_set(conn, mid, {"direction": "received", "set_type": "rfp", "set_label": "Deficient one",
                                    "status": "deficient"})
    labels = [r["set_label"] for r in to_chase(conn, today=dt.date(2026, 11, 26))]
    assert labels == ["Late RFPs", "Deficient one"]
    conn.execute("UPDATE matters SET status = 'closed' WHERE id = ?", (mid,))
    assert to_chase(conn, today=dt.date(2026, 11, 26)) == []


def test_validation(conn, matter, settings):
    mid, party = matter
    other = create_matter(conn, {"short_name": "Other", "caption": "O", "side": "employer"}, settings.matters_dir)
    for bad, message in [({"direction": "sideways", "set_type": "rfp"}, "served this set"),
                         ({"direction": "served", "set_type": "letters"}, "what kind"),
                         ({"direction": "served", "set_type": "rfp", "served_on": "Nov 2"}, "YYYY-MM-DD"),
                         ({"direction": "served", "set_type": "rfp", "request_count": "lots"}, "whole number"),
                         ({"direction": "served", "set_type": "rfp", "party_matter_party_id": party}, "not in this matter")]:
        with pytest.raises(DiscoveryError, match=message):
            save_set(conn, other if "party_matter_party_id" in bad else mid, bad)


def test_discovery_pages(settings, matter, tmp_path):
    from lpc.ingest import ingest_file
    from lpc.matters import get_matter, matter_folder

    mid, party = matter
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    conn = client.app.state.conn
    page = client.get(f"/matters/{mid}/discovery").text
    assert "Add a discovery set" in page and "Received (we must respond)" in page
    assert f'href="/matters/{mid}/discovery"' in client.get(f"/matters/{mid}").text

    r = client.post(f"/matters/{mid}/discovery", data={**received_rogs(party), "party_matter_party_id": str(party),
                                                       "request_count": "30"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Defendant&#39;s First Interrogatories" in page and "Wed Dec 9, 2026" in page and "deadline to confirm" in page
    assert "served on us by Fictional Logistics, Inc.:\n      30 of 25, over the limit" in page

    set_id = conn.execute("SELECT id FROM discovery_sets").fetchone()[0]
    r = client.post(f"/discovery/{set_id}/status", data={"status": "deficient"}, follow_redirects=False)
    assert r.status_code == 303
    dash = client.get("/").text
    assert "Discovery to chase" in dash and "Deficient" in dash

    r = client.post(f"/matters/{mid}/discovery", data={"direction": "received", "set_type": "subpoena",
                                                       "served_on": "2026-11-10"}, follow_redirects=False)
    assert "note=" in r.headers["location"] and "enter the response date" in client.get(r.headers["location"]).text
    r = client.post(f"/matters/{mid}/discovery", data={"direction": "nowhere", "set_type": "rfp"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 400

    assert "Edit Defendant" in client.get(f"/discovery/{set_id}/edit").text
    deadline_id = conn.execute("SELECT deadline_id FROM discovery_sets WHERE id = ?", (set_id,)).fetchone()[0]
    client.post(f"/discovery/{set_id}/delete")
    assert conn.execute("SELECT COUNT(*) FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()[0] == 1   # kept

    m = get_matter(conn, mid)
    src = tmp_path / "Defendant's Second RFPs.txt"
    src.write_text("REQUEST NO. 1: All timesheets.")
    doc = ingest_file(conn, m, matter_folder(m, settings.matters_dir), src, source="opposing").id
    assert f"/matters/{mid}/discovery?request_doc={doc}#add" in client.get(f"/documents/{doc}").text
    page = client.get(f"/matters/{mid}/discovery?request_doc={doc}").text
    assert f'<option value="{doc}" selected>' in page and 'value="Defendant&#39;s Second RFPs"' in page


def test_passed_soft_reminders_leave_the_dashboard_for_the_chase_list(settings, matter, conn):
    from lpc.matters import list_matters

    mid, party = matter
    save_set(conn, mid, {"direction": "served", "set_type": "rfp", "set_label": "Late RFPs", "party_matter_party_id": party,
                         "served_on": "2026-01-05", "service_method": "electronic"})          # their answers long overdue
    conn.commit()
    assert list_matters(conn)[0]["next_deadline"] is None
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    page = client.get("/").text
    upcoming = page.split("Upcoming deadlines")[1].split("Discovery to chase")[0]
    assert "Late RFPs" not in upcoming and "Late RFPs" in page.split("Discovery to chase")[1]
