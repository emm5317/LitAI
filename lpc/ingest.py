"""Document ingestion: copy into the matter folder, hash, extract page text, index for search.

PDF text is extracted with PyMuPDF page by page so every hit maps to a page number
(the same [Page N] convention the Corpus Tools bundles use). Pages with almost no
text are flagged low_text for the local OCR step (OCRmyPDF), which is a later phase.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from .matters import log_change

LOW_TEXT_CHARS_PER_PAGE = 40
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".eml"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pages(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pymupdf as fitz

        with fitz.open(path) as pdf:
            return [page.get_text("text") for page in pdf]
    if suffix in TEXT_SUFFIXES:
        return [path.read_text(encoding="utf-8", errors="replace")]
    return []


def ingest_file(conn: sqlite3.Connection, matter: sqlite3.Row, src: Path, *, title: str | None = None,
                doc_type: str = "other", source: str | None = None, subfolder: str = "10 Source") -> int:
    """Copy `src` into the matter folder (unless already there), record it, and index its text."""
    matter_root = Path(matter["folder_path"])
    digest = sha256_file(src)
    existing = conn.execute(
        "SELECT id FROM documents WHERE matter_id = ? AND sha256 = ?", (matter["id"], digest)
    ).fetchone()
    if existing:
        return existing["id"]

    try:
        rel = src.resolve().relative_to(matter_root.resolve())
        dest = src
    except ValueError:
        dest_dir = matter_root / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
            n += 1
        shutil.copy2(src, dest)
        rel = dest.relative_to(matter_root)

    pages = extract_pages(dest)
    if not pages:
        text_status = "not_applicable"
    else:
        avg = sum(len(p.strip()) for p in pages) / len(pages)
        text_status = "low_text" if avg < LOW_TEXT_CHARS_PER_PAGE else "extracted"

    cur = conn.execute(
        "INSERT INTO documents (matter_id, title, doc_type, source, file_path, sha256, page_count, text_status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (matter["id"], title or src.stem, doc_type, source, str(rel), digest, len(pages) or None, text_status),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO document_pages (document_id, page_no, text) VALUES (?, ?, ?)",
        [(doc_id, i, text) for i, text in enumerate(pages, start=1) if text.strip()],
    )
    log_change(conn, matter["id"], "documents", doc_id, f"Added document {title or src.name}", actor="system")
    conn.commit()
    return doc_id


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
