"""Document ingestion: copy into the matter folder, hash, extract page text, index for search.

Text comes from lpc/extract.py, page by page, so every search hit maps to a page number
(the same [Page N] convention the Corpus Tools bundles use). Documents whose pages average
almost no text are flagged low_text for OCR, which is a later step.

Email attachments are saved next to the email, in a "<email name> - attachments" folder, and
indexed as their own documents with parent_id pointing at the email.
"""

from __future__ import annotations

import hashlib
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


def _free_path(folder: Path, name: str) -> Path:
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
        dest = _free_path(dest_dir, src.name)
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
    log_change(conn, matter["id"], "documents", doc_id, f"Added document {title}", actor="system")
    conn.commit()

    if ex.attachments and _depth < MAX_ATTACHMENT_DEPTH:
        folder = dest.parent / safe_name(f"{dest.stem} - attachments")
        for name, data in ex.attachments:
            if _existing(conn, matter["id"], hashlib.sha256(data).hexdigest()):
                continue                       # already in the matter; do not write a second copy
            folder.mkdir(parents=True, exist_ok=True)
            path = _free_path(folder, safe_name(Path(name).name) or "attachment")
            path.write_bytes(data)
            ingest_file(conn, matter, matter_root, path, source=source, parent_id=doc_id, _depth=_depth + 1)
    return Ingested(doc_id, True)


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: each word quoted, implicit AND; a trailing * keeps prefix search."""
    terms = []
    for word in text.split():
        prefix = word.endswith("*")
        word = word.rstrip("*").replace('"', "")
        if word:
            terms.append(f'"{word}"' + ("*" if prefix else ""))
    return " ".join(terms)


def search(conn: sqlite3.Connection, text: str, matter_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
    query = _fts_query(text)
    if not query:
        return []
    sql = (
        "SELECT d.id AS document_id, d.title, d.matter_id, m.short_name, dp.page_no,"
        # \x02/\x03 mark hits; the UI escapes the text first, then turns them into <mark>
        " snippet(document_pages_fts, 0, char(2), char(3), ' ... ', 16) AS snippet"
        " FROM document_pages_fts"
        " JOIN document_pages dp ON dp.rowid = document_pages_fts.rowid"
        " JOIN documents d ON d.id = dp.document_id"
        " JOIN matters m ON m.id = d.matter_id"
        " WHERE document_pages_fts MATCH ?"
    )
    params: list = [query]
    if matter_id is not None:
        sql += " AND d.matter_id = ?"
        params.append(matter_id)
    sql += " ORDER BY bm25(document_pages_fts) LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
