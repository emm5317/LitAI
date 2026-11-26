from fastapi.testclient import TestClient

from lpc.app import create_app


def make_client(settings) -> TestClient:
    # The app only answers requests addressed to 127.0.0.1 or localhost.
    return TestClient(create_app(settings), base_url="http://127.0.0.1")


def test_end_to_end(settings):
    client = make_client(settings)
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


def test_refuses_outside_hosts_and_cross_site_posts(settings):
    client = make_client(settings)
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403   # DNS rebinding
    form = {"short_name": "X", "caption": "X v. Y", "side": "employee"}
    r = client.post("/matters", data=form, headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/matters", data=form, headers={"Origin": "http://localhost:3000"}, follow_redirects=False)
    assert r.status_code == 403                                                    # other local app
    r = client.post("/matters", data=form, headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/matters", data=form, headers={"Origin": "http://127.0.0.1"}, follow_redirects=False)
    assert r.status_code == 303                                                    # our own page
    assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_no_api_docs(settings):
    client = make_client(settings)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_routes_hold_the_database_lock(settings, monkeypatch):
    import lpc.app

    seen = []
    real = lpc.app.list_matters

    def spy(conn):
        seen.append(app.state.db_lock.locked())
        return real(conn)

    monkeypatch.setattr(lpc.app, "list_matters", spy)
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/").status_code == 200
    assert seen == [True] and not app.state.db_lock.locked()


def _open_matter(client, **over) -> str:
    data = {"short_name": "Doe v. Acme", "caption": "Jane Doe v. Acme Logistics, Inc.", "side": "employer",
            "forum_type": "federal_district"}
    data.update(over)
    r = client.post("/matters", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


HX = {"HX-Request": "true"}


def test_deadline_form_errors_are_shown_not_crashed(settings):
    client = make_client(settings)
    mid = _open_matter(client)
    url = f"/matters/{mid}/deadlines/compute"

    r = client.post(url, data={"entry_id": "frcp.answer", "trigger_date": "11/05/2026"}, headers=HX)
    assert r.status_code == 400 and r.headers["HX-Retarget"] == "#flash"
    assert "YYYY-MM-DD" in r.text and "flash-error" in r.text

    r = client.post(url, data={"entry_id": "no.such.rule", "trigger_date": "2026-11-05"}, headers=HX)
    assert r.status_code == 400 and "not part of this matter" in r.text

    # an AAA rule cannot be used on an N.D. Ill. matter
    r = client.post(url, data={"entry_id": "aaa.answer", "trigger_date": "2026-11-05"}, headers=HX)
    assert r.status_code == 400 and "(ndil)" in r.text

    r = client.post(url, data={"entry_id": "frcp.answer"}, headers=HX)
    assert r.status_code == 422 and "Please fill in: trigger date." in r.text

    assert client.get(f"/matters/{mid}").text.count("deadline status-") == 0   # nothing was stored


def test_missing_pages_show_an_error_page(settings):
    client = make_client(settings)
    r = client.get("/matters/999")
    assert r.status_code == 404 and "Matter not found" in r.text and "Go back" in r.text
    assert "does not exist" in client.get("/nope").text


def test_unexpected_errors_are_reported(settings, monkeypatch):
    import lpc.app

    def boom(conn):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(lpc.app, "list_matters", boom)
    client = TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False)
    r = client.get("/", headers=HX)
    assert r.status_code == 500 and "disk on fire" in r.text


def test_opening_a_matter_is_all_or_nothing(settings, monkeypatch):
    import sqlite3

    import lpc.app

    client = make_client(settings)
    _open_matter(client)
    r = client.post("/matters", data={"short_name": "Doe v. Acme", "caption": "again", "side": "employee"})
    assert r.status_code == 400 and "already exists" in r.text

    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated failure while adding parties")

    monkeypatch.setattr(lpc.app, "add_party_to_matter", fail)
    r = client.post("/matters", data={"short_name": "Roe v. Acme", "caption": "Roe v. Acme", "side": "employee",
                                      "client_name": "Rita Roe"})
    assert r.status_code == 400
    assert client.app.state.conn.execute(
        "SELECT COUNT(*) FROM matters WHERE short_name = 'Roe v. Acme'").fetchone()[0] == 0


def test_edit_matter_saves_and_logs_changes(settings):
    from lpc.ai.gateway import AIRequest, check_policy

    client = make_client(settings)
    mid = _open_matter(client, case_number="1:26-cv-01234")
    assert "Edit Doe v. Acme" in client.get(f"/matters/{mid}/edit").text
    form = {"caption": "Jane Doe v. Acme Logistics, Inc.", "side": "employer", "matter_type": "single_plaintiff",
            "forum_type": "federal_district", "rules_profile": "ndil", "case_number": "1:26-cv-01234",
            "status": "active", "phase": "discovery", "judge": "Hon. A. Judge", "ai_permitted": "1"}
    r = client.post(f"/matters/{mid}/edit", data=form, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/matters/{mid}").text
    assert "Changed phase: (blank) -&gt; discovery" in page and "AI consent turned on" in page
    conn = client.app.state.conn
    assert check_policy(conn, AIRequest(purpose="qa", prompt="x", matter_id=int(mid))) is None

    logged = conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0]
    client.post(f"/matters/{mid}/edit", data=form)                       # nothing changed
    assert conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0] == logged

    client.post(f"/matters/{mid}/edit", data={**form, "status": "closed"})
    assert conn.execute("SELECT closed_on FROM matters WHERE id = ?", (mid,)).fetchone()[0]
    client.post(f"/matters/{mid}/edit", data={**form, "status": "active"})
    assert conn.execute("SELECT closed_on FROM matters WHERE id = ?", (mid,)).fetchone()[0] is None


def test_edit_matter_rejects_bad_values(settings):
    client = make_client(settings)
    mid = _open_matter(client)
    base = {"caption": "C", "side": "employer", "matter_type": "single_plaintiff",
            "forum_type": "federal_district", "status": "active"}
    r = client.post(f"/matters/{mid}/edit", data={**base, "phase": "vibes"})
    assert r.status_code == 400 and "Could not save" in r.text
    r = client.post(f"/matters/{mid}/edit", data={**base, "served_on": "last Tuesday"})
    assert r.status_code == 400 and "served on" in r.text
    r = client.post(f"/matters/{mid}/edit", data={**base, "caption": ""})
    assert r.status_code == 400
    r = client.post(f"/matters/{mid}/edit", data={**base, "rules_profile": "mars"})
    assert r.status_code == 400 and "Unknown deadline rules" in r.text
