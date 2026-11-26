"""FastAPI app: server-rendered pages with HTMX partials."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import __version__
from .config import PACKAGE_DIR, Settings, load_settings
from .db import open_database
from .ingest import ingest_file, search
from .matters import (
    add_party_to_matter, conflicts_search, create_matter, get_matter, list_matters, log_change,
    matter_parties, record_conflict_check,
)
from .rules.engine import SERVICE_METHODS, compute, entries_for, load_rulesets

PROFILE_BY_FORUM = {
    "federal_district": "ndil", "federal_appeals": "ca7", "state_trial": "cook", "state_appeals": "illinois",
    "aaa": "aaa_employment", "jams": "jams_employment",
}


def highlight(snippet: str | None) -> Markup:
    """Escape document text, then mark FTS hits (\\x02...\\x03) with <mark>."""
    text = str(escape(snippet or ""))
    return Markup(text.replace("\x02", "<mark>").replace("\x03", "</mark>"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    conn = open_database(settings.db_path)
    app = FastAPI(title="Law Practice Console", version=__version__)
    app.state.settings = settings
    app.state.conn = conn
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["highlight"] = highlight
    templates.env.globals["version"] = __version__

    def db() -> sqlite3.Connection:
        return app.state.conn

    def matter_or_404(matter_id: int) -> sqlite3.Row:
        m = get_matter(db(), matter_id)
        if m is None:
            raise HTTPException(404, "Matter not found")
        return m

    # ------------------------------------------------------------------ pages
    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        upcoming = db().execute(
            "SELECT d.*, m.short_name FROM deadlines d JOIN matters m ON m.id = d.matter_id"
            " WHERE d.status IN ('proposed', 'confirmed') ORDER BY d.due_on LIMIT 25"
        ).fetchall()
        return templates.TemplateResponse(request, "dashboard.html", {
            "matters": list_matters(db()), "upcoming": upcoming, "today": dt.date.today().isoformat(),
        })

    @app.get("/matters/new", response_class=HTMLResponse)
    def new_matter(request: Request):
        return templates.TemplateResponse(request, "matter_new.html", {"profiles": load_rulesets()})

    @app.post("/matters")
    def create(
        short_name: str = Form(...), caption: str = Form(...), side: str = Form(...),
        matter_type: str = Form("single_plaintiff"), forum_type: str = Form("federal_district"),
        forum_name: str = Form(""), rules_profile: str = Form(""), case_number: str = Form(""),
        judge: str = Form(""), client_name: str = Form(""), client_kind: str = Form("organization"),
        adverse_names: str = Form(""), opposing_counsel: str = Form(""), ai_permitted: str = Form(""),
        summary: str = Form(""), conflict_check_id: str = Form(""),
    ):
        data = {
            "short_name": short_name.strip(), "caption": caption.strip(), "side": side,
            "matter_type": matter_type, "forum_type": forum_type, "forum_name": forum_name,
            "rules_profile": rules_profile or PROFILE_BY_FORUM.get(forum_type), "case_number": case_number,
            "judge": judge, "ai_permitted": 1 if ai_permitted else 0, "summary": summary, "status": "active",
        }
        try:
            matter_id = create_matter(db(), data, settings.matters_dir)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(400, f"Could not open matter: {exc}") from exc
        if client_name.strip():
            add_party_to_matter(db(), matter_id, client_name, "client", kind=client_kind)
        for name in filter(None, (n.strip() for n in adverse_names.splitlines())):
            add_party_to_matter(db(), matter_id, name, "adverse_party",
                                kind="person" if side == "employer" else "organization")
        for name in filter(None, (n.strip() for n in opposing_counsel.splitlines())):
            add_party_to_matter(db(), matter_id, name, "opposing_counsel")
        if conflict_check_id.isdigit():
            db().execute("UPDATE conflict_checks SET matter_id = ? WHERE id = ?", (matter_id, int(conflict_check_id)))
        db().commit()
        return RedirectResponse(f"/matters/{matter_id}", status_code=303)

    @app.get("/matters/{matter_id}", response_class=HTMLResponse)
    def matter_detail(request: Request, matter_id: int):
        m = matter_or_404(matter_id)
        c = db()
        profile = m["rules_profile"]
        return templates.TemplateResponse(request, "matter.html", {
            "m": m,
            "parties": matter_parties(c, matter_id),
            "documents": c.execute("SELECT * FROM documents WHERE matter_id = ? ORDER BY created_at DESC",
                                   (matter_id,)).fetchall(),
            "deadlines": c.execute("SELECT * FROM deadlines WHERE matter_id = ? AND status != 'vacated'"
                                   " ORDER BY due_on", (matter_id,)).fetchall(),
            "entries": entries_for(profile) if profile in load_rulesets() else [],
            "service_methods": SERVICE_METHODS,
            "rulesets": load_rulesets(),
            "changes": c.execute("SELECT * FROM changelog WHERE matter_id = ? ORDER BY id DESC LIMIT 15",
                                 (matter_id,)).fetchall(),
        })

    # --------------------------------------------------------------- partials
    @app.post("/conflicts/check", response_class=HTMLResponse)
    def conflicts(request: Request, names: str = Form("")):
        name_list = [n.strip() for n in names.splitlines() if n.strip()]
        results = conflicts_search(db(), name_list) if name_list else []
        check_id = record_conflict_check(db(), name_list, results) if name_list else None
        return templates.TemplateResponse(request, "partials/conflicts.html",
                                          {"results": results, "names": name_list, "check_id": check_id})

    @app.post("/matters/{matter_id}/parties", response_class=HTMLResponse)
    def add_party(request: Request, matter_id: int, name: str = Form(...), role: str = Form(...),
                  kind: str = Form("person"), title: str = Form("")):
        matter_or_404(matter_id)
        add_party_to_matter(db(), matter_id, name, role, kind=kind, title=title or None)
        db().commit()
        return templates.TemplateResponse(request, "partials/parties.html",
                                          {"parties": matter_parties(db(), matter_id)})

    @app.post("/matters/{matter_id}/documents", response_class=HTMLResponse)
    async def upload(request: Request, matter_id: int, files: list[UploadFile] = File(...),
                     doc_type: str = Form("other")):
        m = matter_or_404(matter_id)
        with tempfile.TemporaryDirectory() as tmp:
            for up in files:
                path = Path(tmp) / Path(up.filename or "upload").name
                with path.open("wb") as fh:
                    shutil.copyfileobj(up.file, fh)
                ingest_file(db(), m, path, doc_type=doc_type, source="internal")
        docs = db().execute("SELECT * FROM documents WHERE matter_id = ? ORDER BY created_at DESC",
                            (matter_id,)).fetchall()
        return templates.TemplateResponse(request, "partials/documents.html", {"documents": docs})

    @app.get("/search", response_class=HTMLResponse)
    def search_view(request: Request, q: str = "", matter_id: int | None = None):
        hits = search(db(), q, matter_id) if q.strip() else []
        return templates.TemplateResponse(request, "partials/search_results.html", {"hits": hits, "q": q})

    @app.post("/matters/{matter_id}/deadlines/compute", response_class=HTMLResponse)
    def compute_deadline(request: Request, matter_id: int, entry_id: str = Form(...),
                         trigger_date: str = Form(...), service_method: str = Form("")):
        m = matter_or_404(matter_id)
        comp = compute(entry_id, dt.date.fromisoformat(trigger_date), service_method or None,
                       profile=m["rules_profile"])
        cur = db().execute(
            "INSERT INTO deadlines (matter_id, title, due_on, rule_id, rule_cite, trigger_event, trigger_date,"
            " service_method, computation, hard, status, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', 'rules_engine')",
            (matter_id, comp.entry.title, comp.due.isoformat(), comp.entry.id, comp.entry.cite, comp.entry.trigger,
             trigger_date, service_method or None, comp.explanation + (" " + " ".join(comp.warnings) if comp.warnings else ""),
             1 if comp.entry.hard else 0),
        )
        log_change(db(), matter_id, "deadlines", cur.lastrowid, f"Proposed deadline: {comp.entry.title} {comp.due}",
                   actor="system")
        db().commit()
        return _deadlines_partial(request, matter_id)

    @app.post("/deadlines/{deadline_id}/{action}", response_class=HTMLResponse)
    def deadline_action(request: Request, deadline_id: int, action: str):
        status = {"confirm": "confirmed", "done": "done", "vacate": "vacated"}.get(action)
        if not status:
            raise HTTPException(404)
        row = db().execute("SELECT matter_id, title FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        db().execute(
            "UPDATE deadlines SET status = ?, confirmed_at = CASE WHEN ? = 'confirmed'"
            " THEN strftime('%Y-%m-%dT%H:%M:%SZ', 'now') ELSE confirmed_at END WHERE id = ?",
            (status, status, deadline_id),
        )
        log_change(db(), row["matter_id"], "deadlines", deadline_id, f"Deadline {status}: {row['title']}")
        db().commit()
        return _deadlines_partial(request, row["matter_id"])

    def _deadlines_partial(request: Request, matter_id: int):
        rows = db().execute("SELECT * FROM deadlines WHERE matter_id = ? AND status != 'vacated' ORDER BY due_on",
                            (matter_id,)).fetchall()
        return templates.TemplateResponse(request, "partials/deadlines.html", {"deadlines": rows})

    return app
