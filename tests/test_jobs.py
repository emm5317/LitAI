import threading
import time

import pytest
from fastapi.testclient import TestClient

from lpc.app import create_app
from lpc.db import open_database
from lpc.jobs import enqueue, requeue_interrupted, run_pending, start_job_worker

HX = {"HX-Request": "true"}


@pytest.fixture
def client(settings):
    c = TestClient(create_app(settings), base_url="http://127.0.0.1")
    r = c.post("/matters", data={"short_name": "Doe v. Acme", "caption": "Doe v. Acme", "side": "employee"},
               follow_redirects=False)
    c.mid = r.headers["location"].rsplit("/", 1)[1]
    c.root = settings.matters_dir / "Doe v. Acme"
    return c


def run_jobs(client, settings):
    # the worker uses its own connection; do the same here
    worker_conn = open_database(settings.db_path)
    try:
        return run_pending(worker_conn, settings.matters_dir)
    finally:
        worker_conn.close()


def docs(client):
    return client.app.state.conn.execute("SELECT * FROM documents ORDER BY id").fetchall()


def test_upload_is_queued_then_indexed(client, settings):
    r = client.post(f"/matters/{client.mid}/documents", data={"doc_type": "policy", "source": "client"},
                    files=[("files", ("handbook.txt", b"Meal breaks are unpaid.")),
                           ("files", ("C:\\Users\\me\\old browser.txt", b"Overtime needs approval."))])
    assert "Upload of 2 files" in r.text and "every 1s" in r.text           # panel polls while running
    assert docs(client) == []
    assert run_jobs(client, settings) == 1

    r = client.get(f"/matters/{client.mid}/jobs?polling=1")
    assert "2 added, 0 already in the matter" in r.text
    assert r.headers["HX-Trigger"] == "documents-changed" and "every 1s" not in r.text
    rows = docs(client)
    assert [d["title"] for d in rows] == ["handbook", "old browser"]
    assert {d["source"] for d in rows} == {"client"} and {d["doc_type"] for d in rows} == {"policy"}
    assert not any((settings.data_dir / "staging").iterdir())               # staging cleaned up
    assert "handbook" in client.get(f"/matters/{client.mid}/documents").text


def test_import_folder_copies_tree_and_skips_junk(client, settings, tmp_path):
    src = tmp_path / "Client docs"
    (src / "Payroll").mkdir(parents=True)
    (src / "offer.txt").write_text("Offer letter: exempt salary.")
    (src / "Payroll" / "2025.csv").write_text("week,hours\n1,52")
    (src / "Payroll" / "dup.txt").write_text("Offer letter: exempt salary.")   # same contents as offer.txt
    (src / "~$offer.docx").write_text("lock file")
    (src / "Thumbs.db").write_text("junk")
    (src / ".hidden").write_text("junk")

    r = client.post(f"/matters/{client.mid}/import", data={"folder": f'"{src}"', "doc_type": "other"})
    assert r.status_code == 200 and "Import of folder Client docs" in r.text
    run_jobs(client, settings)
    assert "2 added, 1 already in the matter" in client.get(f"/matters/{client.mid}/jobs").text
    paths = sorted(d["file_path"].replace("\\", "/") for d in docs(client))
    # files run in sorted order, so Payroll/dup.txt is kept and offer.txt (same contents) is skipped
    assert paths == ["10 Source/Client docs/Payroll/2025.csv", "10 Source/Client docs/Payroll/dup.txt"]
    assert (client.root / "10 Source" / "Client docs" / "Payroll" / "2025.csv").is_file()


def test_import_folder_refuses_bad_paths(client, settings, tmp_path):
    url = f"/matters/{client.mid}/import"
    r = client.post(url, data={"folder": str(tmp_path / "nope")}, headers=HX)
    assert r.status_code == 400 and "Could not find the folder" in r.text
    r = client.post(url, data={"folder": str(client.root / "10 Source")}, headers=HX)
    assert r.status_code == 400 and "Use Rescan" in r.text
    r = client.post(url, data={"folder": str(settings.matters_dir)}, headers=HX)
    assert r.status_code == 400 and "contains this matter" in r.text


def test_rescan_picks_up_files_added_in_explorer(client, settings):
    (client.root / "00 Record" / "order.txt").write_text("Minute entry: fact discovery closes March 1.")
    (client.root / "00 Record" / "~$draft.docx").write_text("lock file")
    client.post(f"/matters/{client.mid}/rescan")
    run_jobs(client, settings)
    assert [d["file_path"].replace("\\", "/") for d in docs(client)] == ["00 Record/order.txt"]   # indexed in place

    client.post(f"/matters/{client.mid}/rescan")
    run_jobs(client, settings)
    assert "No new files found." in client.get(f"/matters/{client.mid}/jobs").text
    assert len(docs(client)) == 1


def test_one_bad_file_does_not_stop_the_job(client, settings, tmp_path, monkeypatch):
    import lpc.jobs

    real = lpc.jobs.ingest_file

    def flaky(conn, matter, root, path, **kw):
        if path.name == "bad.txt":
            raise PermissionError("file is open in another program")
        return real(conn, matter, root, path, **kw)

    monkeypatch.setattr(lpc.jobs, "ingest_file", flaky)
    src = tmp_path / "batch"
    src.mkdir()
    (src / "bad.txt").write_text("x")
    (src / "good.txt").write_text("y")
    (src / "broken.docx").write_bytes(b"PK\x03\x04 not a docx")
    client.post(f"/matters/{client.mid}/import", data={"folder": str(src)})
    run_jobs(client, settings)
    panel = client.get(f"/matters/{client.mid}/jobs").text
    assert "2 added, 0 already in the matter, 1 without readable text, 1 could not be added" in panel
    assert "bad.txt: PermissionError: file is open in another program" in panel
    assert "broken.docx: Could not read the text" in panel


def test_interrupted_jobs_start_over(conn, settings):
    from lpc.matters import create_matter

    mid = create_matter(conn, {"short_name": "M", "caption": "M", "side": "employee"}, settings.matters_dir)
    job = enqueue(conn, "rescan_matter", mid, "Rescan", {})
    conn.execute("UPDATE jobs SET status = 'running', done_count = 3 WHERE id = ?", (job,))
    requeue_interrupted(conn)
    assert tuple(conn.execute("SELECT status, done_count FROM jobs").fetchone()) == ("queued", 0)


def test_worker_thread_runs_queued_jobs(client, settings):
    signal = threading.Event()
    start_job_worker(settings.db_path, settings.matters_dir, signal)
    (client.root / "10 Source" / "note.txt").write_text("hello")
    client.post(f"/matters/{client.mid}/rescan")
    signal.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not docs(client):
        time.sleep(0.05)
    assert [d["title"] for d in docs(client)] == ["note"]
