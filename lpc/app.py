"""FastAPI app: server-rendered pages with HTMX partials."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import json
import mimetypes
import re
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .backup import list_backups
from .chronology import (
    SIGNIFICANCE, EventError, accept_event, chronology, delete_event, event_people_ids, format_event_date, issue_tags,
    parse_event_date, pin_page, reject_event, save_event,
)
from .config import PACKAGE_DIR, Settings, load_settings
from .exports import chronology_csv, chronology_docx, chronology_xlsx, export_name, witness_outline_docx
from .db import open_database
from .ingest import DOC_FIELDS, _fts_query, free_path, read_as_transcript, search, search_titles, update_document
from .jobs import enqueue
from .matters import (
    EDITABLE_FIELDS, FORUM_TYPES, MATTER_TYPES, PHASES, STATUSES, MatterError, add_party_to_matter,
    conflicts_search, create_matter, get_matter, list_matters, log_change, matter_folder, matter_parties,
    record_conflict_check, update_matter,
)
from .transcripts import cite, is_transcript, line_for_terms, lines_on, pdf_page_for
from .rules.engine import SERVICE_METHODS, compute, entries_for, load_rulesets

PROFILE_BY_FORUM = {
    "federal_district": "ndil", "federal_appeals": "ca7", "state_trial": "cook", "state_appeals": "illinois",
    "aaa": "aaa_employment", "jams": "jams_employment",
}


MANUAL_SOURCES = {"court_order": "court order", "stipulation": "stipulation", "manual": "entered by hand"}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def parse_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(400, f"Enter the {label} as a date (YYYY-MM-DD).") from None


# Files that would run a program rather than open a document. "Open" refuses them.
RUNNABLE_SUFFIXES = {".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".ps1", ".vbs", ".vbe", ".js", ".jse",
                     ".wsf", ".hta", ".jar", ".lnk", ".url", ".reg", ".cpl", ".pif"}
PAGES_SHOWN = 50
DOC_TYPES = ("complaint", "answer", "pleading", "motion", "brief", "order", "judgment", "correspondence", "email",
             "discovery_request", "discovery_response", "production", "transcript", "exhibit", "declaration",
             "contract", "policy", "payroll_data", "time_data", "research", "work_product", "demand", "award", "other")
DOC_SOURCES = ("client", "opposing", "court", "third_party", "internal", "email", "public")


def plain_terms(q: str) -> list[str]:
    """The words of a search, without OR, excluded words or quote marks (for marking and cites)."""
    terms = []
    for token in re.findall(r'"[^"]*"|\S+', q):
        if token == "OR" or token.startswith("-"):
            continue
        terms += [word.strip('"*') for word in token.strip('"').split()]
    return [t for t in terms if t]


def mark_terms(text: str, terms: list[str]) -> str:
    """Wrap words that start with a search term in \x02...\x03 (the highlight filter escapes the text,
    then turns those markers into <mark>)."""
    stems = [re.escape(t.casefold()[:max(4, len(t) - 2)]) for t in terms if t]
    if not stems:
        return text
    return re.sub(r"\b(?:" + "|".join(stems) + r")\w*", lambda m: f"\x02{m.group()}\x03", text, flags=re.IGNORECASE)


def source_href(doc_id: int | None, pin: str | None, transcript: bool = False) -> str:
    """Link from a chronology event to the cited spot: a transcript line for 45:3, else a PDF page."""
    if not doc_id:
        return ""
    match = re.match(r"\s*(\d+):(\d+)", pin or "")
    if transcript and match:
        return f"/documents/{doc_id}?tp={match[1]}#t-{match[1]}-{match[2]}"
    page = pin_page(pin)
    return f"/documents/{doc_id}" + (f"?page={page}#p-{page}" if page else "")


def open_in_os(path: Path, reveal: bool = False) -> None:
    """Open a file in its usual program, or show it selected in Explorer (reveal=True)."""
    if sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            os.startfile(path)  # noqa: S606 - the path comes from the documents table, not the request
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if reveal else path)])


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
    app.state.job_signal = threading.Event()     # wakes the job worker started in __main__

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
    templates.env.filters["event_date"] = lambda e: format_event_date(e["event_date"], e["date_precision"])
    templates.env.filters["pin_page"] = pin_page
    templates.env.globals["source_href"] = source_href
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
            "documents": matter_documents(matter_id),
            **jobs_context(matter_id),
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
                     doc_type: str = Form("other"), source: str = Form("")):
        """Save the uploads to a staging folder and queue them; the job worker does the slow part."""
        matter_or_404(matter_id)
        staging = settings.data_dir / "staging" / uuid.uuid4().hex
        staging.mkdir(parents=True)
        for up in files:
            # Browsers send only the file name, but older ones sent the whole Windows path.
            name = PureWindowsPath(up.filename or "upload").name or "upload"
            with free_path(staging, name).open("wb") as fh:
                shutil.copyfileobj(up.file, fh)
        label = f"Upload of {len(files)} file{'s' if len(files) != 1 else ''}"
        enqueue(db(), "ingest_files", matter_id, label, {"dir": str(staging), "doc_type": doc_type, "source": source})
        app.state.job_signal.set()
        return jobs_partial(request, matter_id)

    @app.post("/matters/{matter_id}/import", response_class=HTMLResponse)
    def import_folder(request: Request, matter_id: int, folder: str = Form(...), doc_type: str = Form("other"),
                      source: str = Form("")):
        """Copy a folder (with its subfolders) into the matter's 10 Source and index it."""
        m = matter_or_404(matter_id)
        source_dir = Path(folder.strip().strip('"').strip())      # Explorer's "Copy as path" adds quotes
        if not folder.strip() or not source_dir.is_dir():
            raise HTTPException(400, f"Could not find the folder {folder.strip()!r} on this computer.")
        root = matter_folder(m, settings.matters_dir).resolve()
        src = source_dir.resolve()
        if src == root or root in src.parents:
            raise HTTPException(400, "That folder is already inside this matter's folder. Use Rescan instead.")
        if src in root.parents:
            raise HTTPException(400, "That folder contains this matter's folder. Pick the folder you want to copy in.")
        enqueue(db(), "import_folder", matter_id, f"Import of folder {src.name}",
                {"folder": str(src), "doc_type": doc_type, "source": source})
        app.state.job_signal.set()
        return jobs_partial(request, matter_id)

    @app.post("/matters/{matter_id}/rescan", response_class=HTMLResponse)
    def rescan(request: Request, matter_id: int):
        """Index files you put straight into the matter folder (in Explorer or OneDrive)."""
        matter_or_404(matter_id)
        enqueue(db(), "rescan_matter", matter_id, "Rescan of the matter folder", {})
        app.state.job_signal.set()
        return jobs_partial(request, matter_id)

    @app.get("/matters/{matter_id}/jobs", response_class=HTMLResponse)
    def jobs_view(request: Request, matter_id: int, polling: int = 0):
        matter_or_404(matter_id)
        return jobs_partial(request, matter_id, polling=bool(polling))

    @app.get("/matters/{matter_id}/documents", response_class=HTMLResponse)
    def documents_view(request: Request, matter_id: int):
        matter_or_404(matter_id)
        return templates.TemplateResponse(request, "partials/documents.html", {"documents": matter_documents(matter_id)})

    # -------------------------------------------------------------- documents
    def document_or_404(doc_id: int) -> sqlite3.Row:
        d = db().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if d is None:
            raise HTTPException(404, "Document not found.")
        return d

    def document_path(d: sqlite3.Row) -> Path:
        """The document's file, refusing any stored path that points outside its matter folder."""
        root = matter_folder(get_matter(db(), d["matter_id"]), settings.matters_dir).resolve()
        path = (root / d["file_path"]).resolve()
        if root not in path.parents:
            raise HTTPException(403, "That file is outside the matter folder.")
        if not path.is_file():
            raise HTTPException(404, f"The file is missing from the matter folder: {d['file_path']}")
        return path

    @app.get("/documents/{doc_id}", response_class=HTMLResponse)
    def document_detail(request: Request, doc_id: int, page: int = 1, q: str = "", tp: int | None = None):
        """tp opens a transcript at a transcript page (a condensed transcript has four per PDF page)."""
        d = document_or_404(doc_id)
        transcript = is_transcript(db(), doc_id)
        if transcript and tp:
            page = pdf_page_for(db(), doc_id, tp) or page
        start = max(1, page - 5)
        pages = db().execute("SELECT page_no, text FROM document_pages WHERE document_id = ? AND page_no >= ?"
                             " ORDER BY page_no LIMIT ?", (doc_id, start, PAGES_SHOWN)).fetchall()
        texts = {r["page_no"]: r["text"] for r in pages}
        query = _fts_query(q)
        if query and texts:
            # Mark the search words; the template escapes the text, then turns \x02/\x03 into <mark>.
            for r in db().execute(
                "SELECT dp.page_no, highlight(document_pages_fts, 0, char(2), char(3)) AS text"
                " FROM document_pages_fts JOIN document_pages dp ON dp.rowid = document_pages_fts.rowid"
                " WHERE document_pages_fts MATCH ? AND dp.document_id = ? AND dp.page_no BETWEEN ? AND ?",
                    (query, doc_id, min(texts), max(texts))):
                texts[r["page_no"]] = r["text"]
        transcript_pages: list[dict] = []
        if transcript:
            terms = plain_terms(q)
            for ln in lines_on(db(), doc_id, list(range(start, start + PAGES_SHOWN))):
                if not transcript_pages or transcript_pages[-1]["page"] != ln["page"]:
                    transcript_pages.append({"page": ln["page"], "pdf_page": ln["pdf_page"], "lines": []})
                transcript_pages[-1]["lines"].append({"line": ln["line"], "text": mark_terms(ln["text"], terms)})
        return templates.TemplateResponse(request, "document.html", {
            "transcript_pages": transcript_pages, "tp": tp,
            "d": d, "m": get_matter(db(), d["matter_id"]), "pages": sorted(texts.items()), "page": page, "q": q,
            "start": start, "more": d["page_count"] and start + PAGES_SHOWN <= d["page_count"],
            "parent": db().execute("SELECT id, title FROM documents WHERE id = ?", (d["parent_id"],)).fetchone()
            if d["parent_id"] else None,
            "children": db().execute("SELECT id, title FROM documents WHERE parent_id = ? ORDER BY id",
                                     (doc_id,)).fetchall(),
            "doc_types": DOC_TYPES, "doc_sources": DOC_SOURCES, "pages_shown": PAGES_SHOWN,
        })

    @app.get("/documents/{doc_id}/file")
    def document_file(doc_id: int):
        """The file itself, shown in the window (PDFs open at #page=N)."""
        path = document_path(document_or_404(doc_id))
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")

    @app.post("/documents/{doc_id}/transcript", response_class=HTMLResponse)
    def document_read_transcript(request: Request, doc_id: int):
        """Read page:line numbers from a PDF added before transcripts were recognized (or re-read one)."""
        d = document_or_404(doc_id)
        path = document_path(d)
        if path.suffix.lower() != ".pdf":
            raise HTTPException(400, "Only PDF transcripts can be read for page and line numbers.")
        found = read_as_transcript(db(), doc_id, path)
        if not found:
            raise HTTPException(400, "No numbered transcript pages were found in this PDF.")
        log_change(db(), d["matter_id"], "documents", doc_id, f"Read {d['title']} as a transcript ({found} pages)")
        db().commit()
        return show_notice(request, f"Read {plural(found, 'transcript page')}. Reload to see page and line numbers.")

    @app.post("/documents/{doc_id}/open")
    def document_open(doc_id: int):
        path = document_path(document_or_404(doc_id))
        if path.suffix.lower() in RUNNABLE_SUFFIXES:
            raise HTTPException(400, f"{path.name} is a program, not a document, so it was not opened. "
                                     "Use Show in folder if you need it.")
        open_in_os(path)
        return Response(status_code=204)

    @app.post("/documents/{doc_id}/reveal")
    def document_reveal(doc_id: int):
        open_in_os(document_path(document_or_404(doc_id)), reveal=True)
        return Response(status_code=204)

    @app.post("/documents/{doc_id}/edit")
    async def document_save(request: Request, doc_id: int):
        d = document_or_404(doc_id)
        form = await request.form()
        data = {f: str(form.get(f, "")).strip() for f in DOC_FIELDS}
        data["privileged"] = 1 if form.get("privileged") else 0
        data["key_document"] = 1 if form.get("key_document") else 0
        if not data["title"]:
            raise HTTPException(400, "A document needs a title.")
        for field in ("doc_date", "received_on"):
            if data[field]:
                parse_date(data[field], field.replace("_", " "))
        try:
            update_document(db(), doc_id, data)
            db().commit()
        except sqlite3.IntegrityError as exc:
            db().rollback()
            raise HTTPException(400, f"Could not save the document: {exc}") from exc
        return RedirectResponse(f"/documents/{d['id']}", status_code=303)

    # ------------------------------------------------------------- chronology
    def event_or_404(event_id: int) -> sqlite3.Row:
        e = db().execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if e is None:
            raise HTTPException(404, "Event not found.")
        return e

    def event_form_choices(matter_id: int) -> dict:
        return {
            "parties": matter_parties(db(), matter_id),
            "documents": db().execute("SELECT id, title, doc_date FROM documents WHERE matter_id = ?"
                                      " ORDER BY title COLLATE NOCASE", (matter_id,)).fetchall(),
            "significance": SIGNIFICANCE, "tags": issue_tags(db(), matter_id),
        }

    async def read_event_form(request: Request) -> tuple[dict, list[int]]:
        form = await request.form()
        try:
            value, precision = parse_event_date(str(form.get("event_date", "")), bool(form.get("approximate")))
        except EventError as exc:
            raise HTTPException(400, str(exc)) from None
        doc = str(form.get("source_document_id", ""))
        data = {"event_date": value, "date_precision": precision,
                "description": str(form.get("description", "")).strip(),
                "significance": str(form.get("significance", "supporting")),
                "disputed": 1 if form.get("disputed") else 0,
                "source_document_id": int(doc) if doc.isdigit() else None,
                "pin": str(form.get("pin", "")).strip(), "issue_tags": str(form.get("issue_tags", "")),
                "notes": str(form.get("notes", "")).strip()}
        people = [int(v) for v in form.getlist("people") if str(v).isdigit()]
        return data, people

    def store_event(matter_id: int, data: dict, people: list[int], event_id: int | None = None) -> int:
        try:
            event_id = save_event(db(), matter_id, data, people, event_id)
            db().commit()
            return event_id
        except (EventError, sqlite3.IntegrityError) as exc:
            db().rollback()
            raise HTTPException(400, str(exc)) from exc

    @app.get("/matters/{matter_id}/chronology", response_class=HTMLResponse)
    def chronology_page(request: Request, matter_id: int, person: str = "", issue: str = "", key_only: str = "",
                        disputed_only: str = "", doc: str = "", pin: str = "", text: str = "", date: str = ""):
        """The matter's events. doc/pin/text/date prefill the add form ("Add to chronology" links)."""
        m = matter_or_404(matter_id)
        prefill = {"pin": pin, "description": text[:2000], "event_date": date}
        if doc.isdigit():
            d = db().execute("SELECT id, doc_date FROM documents WHERE id = ? AND matter_id = ?",
                             (int(doc), matter_id)).fetchone()
            if d:
                prefill["source_document_id"] = d["id"]
                prefill["event_date"] = prefill["event_date"] or (d["doc_date"] or "")
        filters = {"person": int(person) if person.isdigit() else None, "issue": issue or None,
                   "key_only": bool(key_only), "disputed_only": bool(disputed_only)}
        return templates.TemplateResponse(request, "chronology.html", {
            "m": m, "events": chronology(db(), matter_id, **filters), "filters": filters,
            "proposed": chronology(db(), matter_id, status="proposed"),
            "prefill": prefill, "adding": bool(doc or pin or text or date),
            "total": db().execute("SELECT COUNT(*) FROM events WHERE matter_id = ? AND status = 'accepted'",
                                  (matter_id,)).fetchone()[0],
            **event_form_choices(matter_id),
        })

    @app.post("/matters/{matter_id}/events")
    async def add_event(request: Request, matter_id: int):
        matter_or_404(matter_id)
        data, people = await read_event_form(request)
        event_id = store_event(matter_id, data, people)
        code = db().execute("SELECT event_code FROM events WHERE id = ?", (event_id,)).fetchone()[0]
        return RedirectResponse(f"/matters/{matter_id}/chronology#{code}", status_code=303)

    @app.get("/events/{event_id}/edit", response_class=HTMLResponse)
    def edit_event(request: Request, event_id: int):
        e = event_or_404(event_id)
        return templates.TemplateResponse(request, "event_edit.html", {
            "e": e, "m": get_matter(db(), e["matter_id"]), "chosen_people": event_people_ids(db(), event_id),
            **event_form_choices(e["matter_id"]),
        })

    @app.post("/events/{event_id}/edit")
    async def save_edited_event(request: Request, event_id: int):
        e = event_or_404(event_id)
        data, people = await read_event_form(request)
        store_event(e["matter_id"], data, people, event_id)
        return RedirectResponse(f"/matters/{e['matter_id']}/chronology#{e['event_code']}", status_code=303)

    @app.post("/events/{event_id}/accept")
    def accept_suggestion(event_id: int):
        e = event_or_404(event_id)
        try:
            code = accept_event(db(), event_id)
        except EventError as exc:
            raise HTTPException(400, str(exc)) from None
        db().commit()
        return RedirectResponse(f"/matters/{e['matter_id']}/chronology#{code}", status_code=303)

    @app.post("/events/{event_id}/reject")
    def reject_suggestion(event_id: int):
        e = event_or_404(event_id)
        try:
            reject_event(db(), event_id)
        except EventError as exc:
            raise HTTPException(400, str(exc)) from None
        db().commit()
        return RedirectResponse(f"/matters/{e['matter_id']}/chronology#proposed", status_code=303)

    @app.post("/documents/{doc_id}/suggest-events", response_class=HTMLResponse)
    def suggest_document_events(request: Request, doc_id: int):
        """Queue an AI read of this document for chronology events (only with the client's consent)."""
        d = document_or_404(doc_id)
        m = get_matter(db(), d["matter_id"])
        if not m["ai_permitted"]:
            raise HTTPException(400, "AI is off for this matter. Turn on client consent under Edit matter first.")
        if not d["page_count"] or d["text_status"] in ("failed", "not_applicable"):
            raise HTTPException(400, "This document has no text to read.")
        enqueue(db(), "suggest_events", m["id"], f"AI event suggestions from {d['title']}", {"document_id": doc_id})
        app.state.job_signal.set()
        return show_notice(request, "Reading the document. Suggestions will appear for review on the Chronology page;"
                                    " progress shows on the matter page.")

    @app.post("/events/{event_id}/delete")
    def remove_event(event_id: int):
        e = event_or_404(event_id)
        delete_event(db(), event_id)
        db().commit()
        return RedirectResponse(f"/matters/{e['matter_id']}/chronology", status_code=303)

    def show_notice(request: Request, message: str, path: Path | None = None):
        """A green message in the page's #flash area (used after saving an export)."""
        return templates.TemplateResponse(request, "partials/notice.html", {"message": message, "path": path},
                                          headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"})

    def save_export(m: sqlite3.Row, description: str, suffix: str, data: bytes) -> Path:
        """Write an export into the matter's 20 Work Product folder, log it, and open it."""
        folder = matter_folder(m, settings.matters_dir) / "20 Work Product"
        folder.mkdir(parents=True, exist_ok=True)
        path = free_path(folder, export_name(m["short_name"], description, suffix))
        path.write_bytes(data)
        log_change(db(), m["id"], "exports", None, f"Exported {description} to 20 Work Product/{path.name}")
        db().commit()
        try:
            open_in_os(path)
        except Exception:      # no program for this file type; the notice still says where it is
            pass
        return path

    @app.post("/matters/{matter_id}/chronology/export", response_class=HTMLResponse)
    def export_chronology(request: Request, matter_id: int, format: str = Form("docx"), person: str = Form(""),
                          issue: str = Form(""), key_only: str = Form(""), disputed_only: str = Form("")):
        m = matter_or_404(matter_id)
        if format not in ("docx", "xlsx", "csv"):
            raise HTTPException(400, f"Unknown export format: {format}.")
        filters = {"person": int(person) if person.isdigit() else None, "issue": issue or None,
                   "key_only": bool(key_only), "disputed_only": bool(disputed_only)}
        events = chronology(db(), matter_id, **filters)
        if not events:
            raise HTTPException(400, "There are no events to export with these filters.")
        described = []
        if filters["person"]:
            described.append(db().execute("SELECT p.name FROM matter_parties mp JOIN parties p ON p.id = mp.party_id"
                                          " WHERE mp.id = ?", (filters["person"],)).fetchone()[0])
        if issue:
            described.append(f"issue: {issue}")
        if key_only:
            described.append("key events only")
        if disputed_only:
            described.append("disputed events only")
        subtitle = f"Filtered: {'; '.join(described)}." if described else ""
        data = {"docx": lambda: chronology_docx(m, events, subtitle), "xlsx": lambda: chronology_xlsx(m, events),
                "csv": lambda: chronology_csv(events)}[format]()
        path = save_export(m, "Chronology", f".{format}", data)
        return show_notice(request, f"Saved {plural(len(events), 'event')} to", path)

    @app.post("/matters/{matter_id}/chronology/witness", response_class=HTMLResponse)
    def export_witness_outline(request: Request, matter_id: int, witness: str = Form("")):
        m = matter_or_404(matter_id)
        row = db().execute("SELECT mp.id, mp.role, mp.title, p.name FROM matter_parties mp"
                           " JOIN parties p ON p.id = mp.party_id WHERE mp.id = ? AND mp.matter_id = ?",
                           (int(witness) if witness.isdigit() else -1, matter_id)).fetchone()
        if row is None:
            raise HTTPException(400, "Choose the person the outline is for.")
        events = chronology(db(), matter_id, person=row["id"])
        path = save_export(m, f"Depo outline - {row['name']}", ".docx", witness_outline_docx(m, row, events))
        return show_notice(request, f"Saved an outline with {plural(len(events), 'event')} to", path)

    @app.get("/search", response_class=HTMLResponse)
    def search_view(request: Request, q: str = "", matter_id: int | None = None, doc_type: str = "",
                    source: str = "", date_from: str = "", date_to: str = "", key_only: str = ""):
        for value, label in ((date_from, "from date"), (date_to, "to date")):
            if value:
                parse_date(value, label)
        filters = {"doc_type": doc_type or None, "source": source or None, "date_from": date_from or None,
                   "date_to": date_to or None, "key_only": bool(key_only)}
        hits = [dict(h) for h in search(db(), q, matter_id, **filters)] if q.strip() else []
        titles = search_titles(db(), q, matter_id, **filters) if q.strip() else []
        terms = plain_terms(q)
        for h in hits:                          # transcripts: which line the hit is on
            h["cite"] = None
            if is_transcript(db(), h["document_id"]):
                found = line_for_terms(lines_on(db(), h["document_id"], [h["page_no"]]), terms)
                h["cite"] = cite(found, found) if found else None
        return templates.TemplateResponse(request, "partials/search_results.html",
                                          {"hits": hits, "titles": titles, "q": q})

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

    def jobs_partial(request: Request, matter_id: int, polling: bool = False):
        """Running jobs plus those finished in the last hour. While any are running the panel
        polls itself; when the last one finishes it tells the document list to refresh."""
        context = jobs_context(matter_id)
        headers = {"HX-Trigger": "documents-changed"} if polling and not context["active"] else None
        return templates.TemplateResponse(request, "partials/jobs.html", context, headers=headers)

    def jobs_context(matter_id: int) -> dict:
        rows = db().execute(
            "SELECT * FROM jobs WHERE matter_id = ? AND (status IN ('queued', 'running')"
            " OR finished_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 hour')) ORDER BY id DESC LIMIT 5",
            (matter_id,)).fetchall()
        jobs = [dict(r, problem_list=json.loads(r["problems"] or "[]")) for r in rows]
        return {"jobs": jobs, "active": any(j["status"] in ("queued", "running") for j in jobs),
                "matter_id": matter_id}

    def matter_documents(matter_id: int) -> list[sqlite3.Row]:
        """Newest first, with each email's attachments listed right under it."""
        return db().execute("SELECT * FROM documents WHERE matter_id = ?"
                            " ORDER BY COALESCE(parent_id, id) DESC, parent_id IS NOT NULL, id",
                            (matter_id,)).fetchall()

    def live_deadlines(matter_id: int) -> list[sqlite3.Row]:
        return db().execute("SELECT * FROM deadlines WHERE matter_id = ? AND status IN ('proposed', 'confirmed', 'done')"
                            " ORDER BY due_on", (matter_id,)).fetchall()

    def _deadlines_partial(request: Request, matter_id: int):
        return templates.TemplateResponse(request, "partials/deadlines.html", {"deadlines": live_deadlines(matter_id)})

    return app
