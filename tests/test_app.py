from fastapi.testclient import TestClient

from lpc.app import create_app


def test_end_to_end(settings):
    client = TestClient(create_app(settings))
    assert client.get("/health").json()["ok"]
    assert "No matters yet" in client.get("/").text
    assert "Open a matter" in client.get("/matters/new").text

    # conflicts check on an empty database
    r = client.post("/conflicts/check", data={"names": "Acme Logistics, Inc.\nJane Doe"})
    assert "No prior appearances" in r.text and 'name="conflict_check_id"' in r.text

    r = client.post("/matters", data={
        "short_name": "Doe v. Acme", "caption": "Jane Doe v. Acme Logistics, Inc.", "side": "employer",
        "forum_type": "federal_district", "client_name": "Acme Logistics, Inc.",
        "adverse_names": "Jane Doe", "opposing_counsel": "Pat Smith", "conflict_check_id": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    page = client.get(url).text
    assert "Doe v. Acme" in page and "ndil" in page and "Answer or Rule 12 motion due" in page

    # second matter: the conflicts check now finds the prior client
    r = client.post("/conflicts/check", data={"names": "ACME Logistics Corp"})
    assert "possible match" in r.text and "client" in r.text

    # deadline: propose, then confirm
    mid = url.rsplit("/", 1)[1]
    r = client.post(f"/matters/{mid}/deadlines/compute",
                    data={"entry_id": "frcp.answer", "trigger_date": "2026-11-05", "service_method": ""})
    assert "2026-11-27" in r.text and "proposed" in r.text
    r = client.post("/deadlines/1/confirm")
    assert "confirmed" in r.text

    # upload and search
    r = client.post(f"/matters/{mid}/documents",
                    files={"files": ("policy.txt", b"Employees must clock in before donning protective gear.")},
                    data={"doc_type": "policy"})
    assert "policy" in r.text
    r = client.get("/search", params={"q": "donning", "matter_id": mid})
    assert "<mark>donning</mark>" in r.text

    # document text is escaped in search results
    client.post(f"/matters/{mid}/documents",
                files={"files": ("x.txt", b"<script>alert(1)</script> overtime")}, data={"doc_type": "other"})
    r = client.get("/search", params={"q": "overtime"})
    assert "<script>" not in r.text

    assert "Upcoming deadlines" in client.get("/").text
