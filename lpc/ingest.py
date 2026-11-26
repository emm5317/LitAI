"""Document ingestion: copy into the matter folder, hash, extract page text, index for search.

Text comes from lpc/extract.py, page by page, so every search hit maps to a page number
(the same [Page N] convention the Corpus Tools bundles use). Documents whose pages average
almost no text are flagged low_text for OCR, which is a later step.

Email attachments are saved next to the email, in a "<email name> - attachments" folder, and
indexed as their own documents with parent_id pointing at the email.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from pathlib import Path
from typing import NamedTuple

from .extract import EMAIL_SUFFIXES, ExtractError, Extracted, extract
from .matters import folder_name as safe_name
from .matters import log_change

LOW_TEXT_CHARS_PER_PAGE = 40
MAX_ATTACHMENT_DEPTH = 3          # an email attached to an email attached to an email


class Ingested(NamedTuple):
    id: int
    added: bool                   # False if the same file was already in the matter


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _existing(conn: sqlite3.Connection, matter_id: int, digest: str) -> int | None:
    row = conn.execute("SELECT id FROM documents WHERE matter_id = ? AND sha256 = ?", (matter_id, digest)).fetchone()
    return row["id"] if row else None


def free_path(folder: Path, name: str) -> Path:
    """folder/name, or folder/name (1), (2)... if that file already exists."""
    dest = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while dest.exists():
        dest = folder / f"{stem} ({n}){suffix}"
        n += 1
    return dest


def ingest_file(conn: sqlite3.Connection, matter: sqlite3.Row, matter_root: Path, src: Path, *,
                title: str | None = None, doc_type: str = "other", source: str | None = None,
                subfolder: str = "10 Source", parent_id: int | None = None, _depth: int = 0) -> Ingested:
    """Copy `src` into the matter folder (unless already there), record it, and index its text.

    `matter_root` is the matter's folder on disk (see matters.matter_folder). A file whose
    contents are already in the matter is skipped and its existing id returned.
    """
    digest = sha256_file(src)
    found = _existing(conn, matter["id"], digest)
    if found:
        return Ingested(found, False)

    try:
        rel = src.resolve().relative_to(matter_root.resolve())
        dest = src
    except ValueError:
        dest_dir = matter_root / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = free_path(dest_dir, src.name)
        shutil.copy2(src, dest)
        rel = dest.relative_to(matter_root)

    problem = None
    try:
        ex = extract(dest)
    except ExtractError as exc:
        ex, problem = Extracted(pages=[]), str(exc)
    if problem:
        text_status = "failed"
    elif not ex.pages:
        text_status = "not_applicable"
    else:
        avg = sum(len(p.strip()) for p in ex.pages) / len(ex.pages)
        text_status = "low_text" if avg < LOW_TEXT_CHARS_PER_PAGE else "extracted"
    if doc_type == "other" and src.suffix.lower() in EMAIL_SUFFIXES:
        doc_type = "email"
    title = title or ex.title or src.stem

    cur = conn.execute(
        "INSERT INTO documents (matter_id, title, doc_type, source, file_path, sha256, page_count, text_status,"
        " author, recipients, doc_date, parent_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (matter["id"], title, doc_type, source, str(rel), digest, len(ex.pages) or None, text_status,
         ex.author, ex.recipients, ex.doc_date, parent_id,
         f"Could not read the text: {problem}" if problem else None),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO document_pages (document_id, page_no, text) VALUES (?, ?, ?)",
        [(doc_id, i, text) for i, text in enumerate(ex.pages, start=1) if text.strip()],
    )
    if dest.suffix.lower() == ".pdf" and text_status == "extracted":
        read_as_transcript(conn, doc_id, dest)
    log_change(conn, matter["id"], "documents", doc_id, f"Added document {title}", actor="system")
    conn.commit()

    if ex.attachments and _depth < MAX_ATTACHMENT_DEPTH:
        folder = dest.parent / safe_name(f"{dest.stem} - attachments")
        for name, data in ex.attachments:
            if _existing(conn, matter["id"], hashlib.sha256(data).hexdigest()):
                continue                       # already in the matter; do not write a second copy
            folder.mkdir(parents=True, exist_ok=True)
            path = free_path(folder, safe_name(Path(name).name) or "attachment")
            path.write_bytes(data)
            ingest_file(conn, matter, matter_root, path, source=source, parent_id=doc_id, _depth=_depth + 1)
    return Ingested(doc_id, True)


def read_as_transcript(conn: sqlite3.Connection, doc_id: int, path: Path) -> int:
    """If the PDF is a deposition transcript, store its page:line numbers (and call it a transcript
    if its type was 'other'). Returns the number of transcript pages found. Does not commit."""
    from .transcripts import parse_transcript, store_transcript

    try:
        lines = parse_transcript(path)
    except Exception:          # an odd PDF should never stop the document being added
        return 0
    if not lines:
        return 0
    store_transcript(conn, doc_id, lines)
    conn.execute("UPDATE documents SET doc_type = 'transcript' WHERE id = ? AND doc_type = 'other'", (doc_id,))
    return len({ln.page for ln in lines})


DOC_FIELDS = ("title", "doc_type", "doc_date", "author", "recipients", "bates_begin", "bates_end", "docket_no",
              "source", "received_on", "privileged", "key_document", "summary", "notes")


def update_document(conn: sqlite3.Connection, doc_id: int, data: dict) -> list[str]:
    """Save edited document details and log one line naming the fields that changed.
    Blank values are stored as NULL. Returns the changed field names. Does not commit."""
    current = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    changes = {f: (None if data[f] in ("", None) else data[f]) for f in DOC_FIELDS if f in data}
    changes = {f: v for f, v in changes.items() if v != current[f]}
    if not changes:
        return []
    assignments = ", ".join(f"{f} = ?" for f in changes)
    conn.execute(f"UPDATE documents SET {assignments} WHERE id = ?", (*changes.values(), doc_id))
    names = [f.replace("_", " ") for f in changes]
    log_change(conn, current["matter_id"], "documents", doc_id,
               f"Updated document {changes.get('title') or current['title']}: {', '.join(names)}")
    return list(changes)


_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def _fts_query(text: str) -> str:
    """Turn what the user typed into a safe FTS5 query. Every word is quoted, so nothing they type
    can be read as FTS syntax; only these are understood:

        night shift      both words, anywhere on the page
        "night shift"    the exact phrase
        overtim*         words starting with "overtim"
        nights OR weekends   either word
        overtime -exempt     pages with "overtime" but not "exempt"
    """
    parts: list[str] = []
    for phrase, word in _TOKEN.findall(text):
        if phrase:
            words = phrase.split()
            if words:
                parts.append('"' + " ".join(words) + '"')
            continue
        if word == "OR":
            if parts and parts[-1] != "OR":
                parts.append("OR")
            continue
        negate = word.startswith("-")
        prefix = word.endswith("*")
        word = word.strip("-*").replace('"', "")
        if not word:
            continue
        term = f'"{word}"' + ("*" if prefix else "")
        if negate:
            if parts and parts[-1] != "OR":          # FTS5 NOT needs something before it
                parts.append(f"NOT {term}")
            continue
        parts.append(term)
    while parts and parts[-1] == "OR":
        parts.pop()
    return " ".join(parts)


def _filters(matter_id, doc_type, source, date_from, date_to, key_only) -> tuple[str, list]:
    sql, params = "", []
    for column, value in (("d.matter_id", matter_id), ("d.doc_type", doc_type), ("d.source", source)):
        if value not in (None, ""):
            sql += f" AND {column} = ?"
            params.append(value)
    if date_from:
        sql += " AND d.doc_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND d.doc_date <= ?"
        params.append(date_to)
    if key_only:
        sql += " AND d.key_document = 1"
    return sql, params


def search(conn: sqlite3.Connection, text: str, matter_id: int | None = None, limit: int = 50, *,
           doc_type: str | None = None, source: str | None = None, date_from: str | None = None,
           date_to: str | None = None, key_only: bool = False) -> list[sqlite3.Row]:
    """Pages matching `text`, best first. Date filters use the document date (YYYY-MM-DD)."""
    query = _fts_query(text)
    if not query:
        return []
    where, params = _filters(matter_id, doc_type, source, date_from, date_to, key_only)
    sql = (
        "SELECT d.id AS document_id, d.title, d.matter_id, d.file_path, d.doc_type, d.doc_date, m.short_name,"
        " dp.page_no,"
        # \x02/\x03 mark hits; the UI escapes the text first, then turns them into <mark>
        " snippet(document_pages_fts, 0, char(2), char(3), ' ... ', 16) AS snippet"
        " FROM document_pages_fts"
        " JOIN document_pages dp ON dp.rowid = document_pages_fts.rowid"
        " JOIN documents d ON d.id = dp.document_id"
        " JOIN matters m ON m.id = d.matter_id"
        " WHERE document_pages_fts MATCH ?" + where +
        " ORDER BY bm25(document_pages_fts) LIMIT ?"
    )
    return conn.execute(sql, [query, *params, limit]).fetchall()


def search_titles(conn: sqlite3.Connection, text: str, matter_id: int | None = None, limit: int = 10,
                  **filters) -> list[sqlite3.Row]:
    """Documents whose title contains every plain word typed (catches files with no text,
    such as scans, spreadsheets and photos). Operators and excluded words are ignored."""
    words = [w.strip('"*').lower() for w in re.findall(r'"[^"]*"|\S+', text)
             if w != "OR" and not w.startswith("-")]
    words = [w for w in words if w]
    if not words:
        return []
    where, params = _filters(matter_id, filters.get("doc_type"), filters.get("source"), filters.get("date_from"),
                             filters.get("date_to"), filters.get("key_only", False))
    likes = " AND ".join("instr(lower(d.title), ?) > 0" for _ in words)
    return conn.execute(
        "SELECT d.id AS document_id, d.title, d.matter_id, d.file_path, d.doc_type, d.doc_date, m.short_name"
        f" FROM documents d JOIN matters m ON m.id = d.matter_id WHERE {likes}{where}"
        " ORDER BY d.doc_date DESC, d.id DESC LIMIT ?", [*words, *params, limit]).fetchall()
