"""FastAPI app: server-rendered pages with HTMX partials."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .backup import list_backups
from .config import PACKAGE_DIR, Settings, load_settings
from .db import open_database
from .ingest import ingest_file, search
from .matters import (
    EDITABLE_FIELDS, FORUM_TYPES, MATTER_TYPES, PHASES, STATUSES, MatterError, add_party_to_matter,
    conflicts_search, create_matter, get_matter, list_matters, log_change, matter_folder, matter_parties,
    record_conflict_check, update_matter,
)
from .rules.engine import SERVICE_METHODS, compute, entries_for, load_rulesets

PROFILE_BY_FORUM = {
    "federal_district": "ndil", "federal_appeals": "ca7", "state_trial": "cook", "state_appeals": "illinois",
    "aaa": "aaa_employment", "jams": "jams_employment",
}


MANUAL_SOURCES = {"court_order": "court order", "stipulation": "stipulation", "manual": "entered by hand"}


def parse_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(400, f"Enter the {label} as a date (YYYY-MM-DD).") from None


def highlight(snippet: str | None) -> Markup:
    """Escape document text, then mark FTS hits (\\x02...\\x03) with <mark>."""
    text = str(escape(snippet or ""))
    return Markup(text.replace("\x02", "<mark>").replace("\x03", "</mark>"))


LOCAL_HOSTS = {"127.0.0.1", "localhost"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def local_only_problem(method: str, headers) -> str | None:
    """Why a request must be refused, or None if it is fine.

    The server listens on 127.0.0.1, but a web page open in your normal browser can still
    send requests to it. Two checks stop that:
      * Host must be 127.0.0.1 or localhost. This defeats "DNS rebinding", where an outside
        site points its own name at 127.0.0.1 to read your data.
      * A POST must come from this app's own page (Origin matches Host). This stops another
        site, or another local program's page, from submitting forms here.
    """
    host = headers.get("host", "")
    if urlsplit(f"//{host}").hostname not in LOCAL_HOSTS:
        return "Requests must be addressed to 127.0.0.1 or localhost."
    if method in UNSAFE_METHODS:
        origin = headers.get("origin")
        if origin and urlsplit(origin).netloc != host:
            return "Form submitted from another site."
        if headers.get("sec-fetch-site") == "cross-site":
            return "Form submitted from another site."
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    conn = open_database(settings.db_path)
    # One connection serves the whole app, and FastAPI runs requests on several threads.
    # Without a lock, one request's commit() could commit another's half-finished writes.
    # Every route holds this lock for its whole run; background jobs open their own connection.
    db_lock = threading.Lock()

    def hold_db_lock():
        with db_lock:
            yield

    # No /docs or /openapi.json: there is no public API to describe.
    app = FastAPI(title="Law Practice Console", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(hold_db_lock)])
    app.state.db_lock = db_lock

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        problem = local_only_problem(request.method, request.headers)
        if problem:
            return PlainTextResponse(f"Refused: {problem}", status_code=403)
        return await call_next(request)

    app.state.settings = settings
    app.state.conn = conn
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["highlight"] = highlight
    templates.env.globals["version"] = __version__

    def db() -> sqlite3.Connection:
        return app.state.conn

    # ----------------------------------------------------------------- errors
    def show_error(request: Request, message: str, status: int):
        """HTMX requests get a dismissable message in the page's #flash area; full pages get an error page."""
        if request.headers.get("hx-request"):
            return templates.TemplateResponse(
                request, "partials/error.html", {"message": message}, status_code=status,
                headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"})
        return templates.TemplateResponse(request, "error.html", {"message": message, "status": status},
                                          status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
        if message == "Not Found":          # Starlette's text for an unknown URL
            message = "That page does not exist."
        return show_error(request, message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def form_error(request: Request, exc: RequestValidationError):
        fields = sorted({str(e["loc"][-1]).replace("_", " ") for e in exc.errors()})
        return show_error(request, "Please fill in: " + ", ".join(fields) + ".", 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return show_error(request, f"Something went wrong ({type(exc).__name__}: {exc}).", 500)

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
        backups = list_backups(settings.backup_dir)
        return templates.TemplateResponse(request, "dashboard.html", {
            "matters": list_matters(db()), "upcoming": upcoming, "today": dt.date.today().isoformat(),
            "last_backup": backups[-1].stem.removeprefix("lpc-") if backups else None,
            "backup_dir": settings.backup_dir,
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
        # The matter, its parties and the conflicts-check link are saved together or not at all.
        try:
            matter_id = create_matter(db(), data, settings.matters_dir, commit=False)
            if client_name.strip():
                add_party_to_matter(db(), matter_id, client_name, "client", kind=client_kind)
            for name in filter(None, (n.strip() for n in adverse_names.splitlines())):
                add_party_to_matter(db(), matter_id, name, "adverse_party",
                                    kind="person" if side == "employer" else "organization")
            for name in filter(None, (n.strip() for n in opposing_counsel.splitlines())):
                add_party_to_matter(db(), matter_id, name, "opposing_counsel")
            if conflict_check_id.isdigit():
                db().execute("UPDATE conflict_checks SET matter_id = ? WHERE id = ?",
                             (matter_id, int(conflict_check_id)))
            db().commit()
        except Exception as exc:
            db().rollback()
            if isinstance(exc, MatterError):
                raise HTTPException(400, str(exc)) from exc
            if isinstance(exc, sqlite3.IntegrityError):
                raise HTTPException(400, f"Could not open the matter: {exc}") from exc
            raise
        return RedirectResponse(f"/matters/{matter_id}", status_code=303)

    @app.get("/matters/{matter_id}", response_class=HTMLResponse)
    def matter_detail(request: Request, matter_id: int):
        m = matter_or_404(matter_id)
        c = db()
        profile = m["rules_profile"]
        return templates.TemplateResponse(request, "matter.html", {
            "m": m,
            "folder": matter_folder(m, settings.matters_dir),
            "parties": matter_parties(c, matter_id),
            "documents": c.execute("SELECT * FROM documents WHERE matter_id = ? ORDER BY created_at DESC",
                                   (matter_id,)).fetchall(),
            "deadlines": live_deadlines(matter_id),
            "entries": entries_for(profile) if profile in load_rulesets() else [],
            "service_methods": SERVICE_METHODS,
            "manual_sources": MANUAL_SOURCES,
            "rulesets": load_rulesets(),
            "changes": c.execute("SELECT * FROM changelog WHERE matter_id = ? ORDER BY id DESC LIMIT 15",
                                 (matter_id,)).fetchall(),
        })

    @app.get("/matters/{matter_id}/edit", response_class=HTMLResponse)
    def edit_matter(request: Request, matter_id: int):
        return templates.TemplateResponse(request, "matter_edit.html", {
            "m": matter_or_404(matter_id), "profiles": load_rulesets(), "statuses": STATUSES, "phases": PHASES,
            "matter_types": MATTER_TYPES, "forum_types": FORUM_TYPES,
        })

    @app.post("/matters/{matter_id}/edit")
    async def save_matter(request: Request, matter_id: int):
        matter_or_404(matter_id)
        form = await request.form()
        data = {field: str(form.get(field, "")).strip() for field in EDITABLE_FIELDS}
        data["ai_permitted"] = 1 if form.get("ai_permitted") else 0      # unticked boxes are not sent
        if data["rules_profile"] and data["rules_profile"] not in load_rulesets():
            raise HTTPException(400, f"Unknown deadline rules: {data['rules_profile']}.")
        for field in ("commenced_on", "served_on"):
            if data[field]:
                parse_date(data[field], field.replace("_", " "))
        try:
            update_matter(db(), matter_id, data)
            db().commit()
        except sqlite3.IntegrityError as exc:
            db().rollback()
            raise HTTPException(400, f"Could not save the matter: {exc}") from exc
        return RedirectResponse(f"/matters/{matter_id}", status_code=303)

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
                ingest_file(db(), m, matter_folder(m, settings.matters_dir), path,
                            doc_type=doc_type, source="internal")
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
        profile = m["rules_profile"]
        if profile not in load_rulesets():
            raise HTTPException(400, "Set a rules profile on this matter before calculating deadlines.")
        if entry_id not in {e.id for e in entries_for(profile)}:
            raise HTTPException(400, f"That rule is not part of this matter's rules ({profile}).")
        if service_method and service_method not in SERVICE_METHODS:
            raise HTTPException(400, f"Unknown service method: {service_method}.")
        trigger = parse_date(trigger_date, "trigger date")
        comp = compute(entry_id, trigger, service_method or None, profile=profile)
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

    @app.post("/matters/{matter_id}/deadlines", response_class=HTMLResponse)
    def add_deadline(request: Request, matter_id: int, title: str = Form(...), due_on: str = Form(...),
                     source: str = Form("court_order"), rule_cite: str = Form(""), notes: str = Form(""),
                     hard: str = Form("")):
        """A deadline you enter yourself (court order, stipulation, or by hand). You typed the date, so it
        is saved as confirmed; computed deadlines still start as proposed."""
        matter_or_404(matter_id)
        if source not in MANUAL_SOURCES:
            raise HTTPException(400, f"Unknown deadline source: {source}.")
        due = parse_date(due_on, "due date")
        cur = db().execute(
            "INSERT INTO deadlines (matter_id, title, due_on, rule_cite, computation, hard, status, source,"
            " confirmed_at, notes) VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?)",
            (matter_id, title.strip(), due.isoformat(), rule_cite.strip() or None,
             f"Entered by hand ({MANUAL_SOURCES[source]}).", 1 if hard else 0, source, notes.strip() or None),
        )
        log_change(db(), matter_id, "deadlines", cur.lastrowid,
                   f"Added deadline ({MANUAL_SOURCES[source]}): {title.strip()} {due}")
        db().commit()
        return _deadlines_partial(request, matter_id)

    @app.post("/deadlines/{deadline_id}/reschedule", response_class=HTMLResponse)
    def reschedule_deadline(request: Request, deadline_id: int, due_on: str = Form(...), reason: str = Form(...)):
        """Move a deadline to a new date. The old row is kept as 'superseded' so the history survives."""
        old = db().execute("SELECT * FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()
        if old is None:
            raise HTTPException(404, "Deadline not found.")
        if old["status"] not in ("proposed", "confirmed"):
            raise HTTPException(400, f"Only open deadlines can be moved (this one is {old['status']}).")
        due = parse_date(due_on, "new date")
        reason = reason.strip()
        if not reason:
            raise HTTPException(400, "Say why the date changed (for example, \"Dkt. 31 extension\").")
        cur = db().execute(
            "INSERT INTO deadlines (matter_id, title, due_on, due_time, rule_id, rule_cite, trigger_event,"
            " trigger_date, service_method, computation, hard, status, source, confirmed_at, notes, supersedes_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, ?)",
            (old["matter_id"], old["title"], due.isoformat(), old["due_time"], old["rule_id"], old["rule_cite"],
             old["trigger_event"], old["trigger_date"], old["service_method"],
             f"Moved from {old['due_on']}: {reason}." + (f" Original: {old['computation']}" if old["computation"] else ""),
             old["hard"], old["source"], old["notes"], deadline_id),
        )
        db().execute("UPDATE deadlines SET status = 'superseded' WHERE id = ?", (deadline_id,))
        log_change(db(), old["matter_id"], "deadlines", cur.lastrowid,
                   f"Moved deadline {old['title']}: {old['due_on']} -> {due} ({reason})")
        db().commit()
        return _deadlines_partial(request, old["matter_id"])

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

    def live_deadlines(matter_id: int) -> list[sqlite3.Row]:
        return db().execute("SELECT * FROM deadlines WHERE matter_id = ? AND status IN ('proposed', 'confirmed', 'done')"
                            " ORDER BY due_on", (matter_id,)).fetchall()

    def _deadlines_partial(request: Request, matter_id: int):
        return templates.TemplateResponse(request, "partials/deadlines.html", {"deadlines": live_deadlines(matter_id)})

    return app
