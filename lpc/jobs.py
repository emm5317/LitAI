"""Background jobs: work too slow for a page request (big uploads, importing a folder, rescanning).

A route adds a row to `jobs` and returns at once. One worker thread, with its own database
connection, runs queued jobs in order and records progress, which the matter page polls.
Ingest skips files already in the matter (same SHA-256), so a job cut short by closing the
app is simply run again on the next start.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path, PurePath

from .db import connect
from .ingest import ingest_file
from .matters import get_matter, matter_folder

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
SKIP_SUFFIXES = {".tmp", ".lnk", ".partial", ".crdownload"}
MAX_PROBLEMS = 50


def skip_file(path: Path) -> bool:
    """Office lock files (~$memo.docx), hidden files, shortcuts and other junk."""
    name = path.name.lower()
    return name.startswith(("~$", ".")) or name in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES


def enqueue(conn: sqlite3.Connection, kind: str, matter_id: int, label: str, params: dict) -> int:
    cur = conn.execute("INSERT INTO jobs (kind, matter_id, label, params) VALUES (?, ?, ?, ?)",
                       (kind, matter_id, label, json.dumps(params)))
    conn.commit()
    return cur.lastrowid


def _files(conn: sqlite3.Connection, job: sqlite3.Row, root: Path) -> list[tuple[Path, str]]:
    """The files a job will ingest, each with the matter subfolder it should be copied into."""
    params = json.loads(job["params"])
    if job["kind"] == "ingest_files":
        return [(p, "10 Source") for p in sorted(Path(params["dir"]).iterdir()) if p.is_file()]
    if job["kind"] == "import_folder":
        folder = Path(params["folder"])
        return [(p, str(PurePath("10 Source", folder.name, p.parent.relative_to(folder))))
                for p in sorted(folder.rglob("*")) if p.is_file() and not skip_file(p)]
    if job["kind"] == "rescan_matter":
        # Only files at paths not yet recorded are hashed, so a rescan stays quick (and does not
        # download every OneDrive placeholder). A file edited in place is not re-read.
        known = {r["file_path"].replace("\\", "/") for r in
                 conn.execute("SELECT file_path FROM documents WHERE matter_id = ?", (job["matter_id"],))}
        return [(p, "") for p in sorted(root.rglob("*"))
                if p.is_file() and not skip_file(p) and p.relative_to(root).as_posix() not in known]
    raise ValueError(f"unknown job kind {job['kind']}")


def _run_suggest_events(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    from .ai.events import suggest_events      # imported here: only this job needs the AI code

    def progress(done: int, total: int) -> None:
        conn.execute("UPDATE jobs SET done_count = ?, total = ? WHERE id = ?", (done, total, job["id"]))
        conn.commit()

    summary, problems = suggest_events(conn, json.loads(job["params"])["document_id"], progress=progress)
    conn.execute(f"UPDATE jobs SET status = 'done', summary = ?, problems = ?, finished_at = {NOW} WHERE id = ?",
                 (summary, json.dumps(problems[:MAX_PROBLEMS]), job["id"]))
    conn.commit()


def run_job(conn: sqlite3.Connection, job: sqlite3.Row, matters_root: Path) -> None:
    if job["kind"] == "suggest_events":
        return _run_suggest_events(conn, job)
    matter = get_matter(conn, job["matter_id"])
    if matter is None:
        raise ValueError("the matter no longer exists")
    root = matter_folder(matter, matters_root)
    params = json.loads(job["params"])
    files = _files(conn, job, root)
    conn.execute("UPDATE jobs SET total = ? WHERE id = ?", (len(files), job["id"]))
    conn.commit()
    added = already = unreadable = failed = 0
    problems: list[str] = []
    for n, (path, subfolder) in enumerate(files, start=1):
        try:
            result = ingest_file(conn, matter, root, path, doc_type=params.get("doc_type") or "other",
                                 source=params.get("source") or None, subfolder=subfolder or "10 Source")
            if not result.added:
                already += 1
            else:
                added += 1
                status = conn.execute("SELECT text_status, notes FROM documents WHERE id = ?", (result.id,)).fetchone()
                if status["text_status"] == "failed":
                    unreadable += 1
                    problems.append(f"{path.name}: {status['notes']}")
        except Exception as exc:              # one bad file must not stop the rest
            conn.rollback()
            failed += 1
            problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
        conn.execute("UPDATE jobs SET done_count = ? WHERE id = ?", (n, job["id"]))
        conn.commit()
    parts = [f"{added} added", f"{already} already in the matter"]
    if unreadable:
        parts.append(f"{unreadable} without readable text")
    if failed:
        parts.append(f"{failed} could not be added")
    summary = ", ".join(parts) if files else "No new files found."
    conn.execute(f"UPDATE jobs SET status = 'done', summary = ?, problems = ?, finished_at = {NOW} WHERE id = ?",
                 (summary, json.dumps(problems[:MAX_PROBLEMS]), job["id"]))
    conn.commit()
    if job["kind"] == "ingest_files":
        shutil.rmtree(params["dir"], ignore_errors=True)


def run_pending(conn: sqlite3.Connection, matters_root: Path) -> int:
    """Run queued jobs, oldest first, until none are left. Returns how many ran."""
    ran = 0
    while True:
        job = conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if job is None:
            return ran
        conn.execute(f"UPDATE jobs SET status = 'running', started_at = {NOW} WHERE id = ?", (job["id"],))
        conn.commit()
        try:
            run_job(conn, job, matters_root)
        except Exception as exc:
            conn.rollback()
            conn.execute(f"UPDATE jobs SET status = 'failed', error = ?, finished_at = {NOW} WHERE id = ?",
                         (f"{type(exc).__name__}: {exc}", job["id"]))
            conn.commit()
        ran += 1


def requeue_interrupted(conn: sqlite3.Connection) -> None:
    """Jobs left 'running' when the app closed start over (ingest skips what is already done)."""
    conn.execute("UPDATE jobs SET status = 'queued', done_count = 0 WHERE status = 'running'")
    conn.commit()


def start_job_worker(db_path: Path, matters_root: Path, signal: threading.Event) -> threading.Thread:
    """Run jobs in the background. Routes call signal.set() after queuing one; the worker also
    checks every few seconds in case a signal was missed."""
    def loop() -> None:
        conn = connect(db_path)
        requeue_interrupted(conn)
        while True:
            try:
                run_pending(conn, matters_root)
            except Exception as exc:
                print(f"Job worker error: {exc}")
            signal.wait(timeout=5)
            signal.clear()

    thread = threading.Thread(target=loop, name="job-worker", daemon=True)
    thread.start()
    return thread
