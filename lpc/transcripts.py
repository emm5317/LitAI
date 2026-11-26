"""Deposition transcripts: read page:line numbers from a transcript PDF so cites can say 45:3-12.

Court reporters lay transcripts out in two ways:
  full-size  one transcript page per PDF page, "Page 45" near the top, lines numbered 1-25 (or 1-22)
             in a left margin column;
  condensed  four transcript pages per sheet ("Min-U-Script"), two columns of two, each with its own
             "Page 45" label and numbered lines.

PDF text does not come out in reading order (one reporter's file puts the "Page 58" label after
the testimony, and numbers before their lines), so this works from word positions: a line number
is the leftmost word on a visual line, inside the region under a page label, and the numbers in a
region must run mostly in sequence. That last test also keeps the word index at the back
("recall 20:7,16 21:3", "12 5:5 36:6") from being read as testimony. ECF stamps ("Page 16 of 56")
are not page labels.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIN_LINES_PER_PAGE = 5
MIN_PAGES = 3                # fewer transcript pages than this: not treated as a transcript
_LINE_TOLERANCE = 3.0        # points; words this close vertically are on the same visual line


@dataclass(frozen=True)
class TLine:
    pdf_page: int            # 1-based page of the PDF file
    page: int                # transcript page
    line: int
    text: str


def _visual_lines(words: list, x_min: float, x_max: float, y_min: float, y_max: float) -> list[list]:
    """Words inside a box, grouped into visual lines (top to bottom), each sorted left to right."""
    inside = [w for w in words if x_min <= w[0] < x_max and y_min <= (w[1] + w[3]) / 2 < y_max]
    inside.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in inside:
        mid = (w[1] + w[3]) / 2
        if lines and abs(mid - (lines[-1][0][1] + lines[-1][0][3]) / 2) <= _LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w[0]) for line in lines]


def _labels(words: list) -> list[tuple[int, float, float]]:
    """(transcript page, x, y) for each "Page N" label; "Page 6 of 549" (an ECF stamp) is skipped."""
    out = []
    for line in _visual_lines(words, -1e9, 1e9, -1e9, 1e9):
        texts = [w[4] for w in line]
        for i, t in enumerate(texts[:-1]):
            if t == "Page" and texts[i + 1].isdigit() and (i + 2 >= len(texts) or texts[i + 2].lower() != "of"):
                out.append((int(texts[i + 1]), line[i][0], (line[i][1] + line[i][3]) / 2))
    return out


def _region_lines(words: list, page_no: int, pdf_page: int, box: tuple[float, float, float, float]) -> list[TLine]:
    numbered = []
    for line in _visual_lines(words, *box):
        first = line[0][4]
        if first.isdigit() and 1 <= int(first) <= 30:
            numbered.append((int(first), " ".join(w[4] for w in line[1:]).strip()))
    if len(numbered) < MIN_LINES_PER_PAGE:
        return []
    in_sequence = sum(1 for a, b in zip(numbered, numbered[1:]) if b[0] == a[0] + 1)
    if numbered[0][0] > 3 or in_sequence < 0.8 * (len(numbered) - 1):
        return []
    return [TLine(pdf_page, page_no, n, text) for n, text in numbered]


def parse_page(words: list, width: float, height: float, pdf_page: int) -> list[TLine]:
    """Transcript lines on one PDF page (empty if it is not a transcript page)."""
    labels = _labels(words)
    if not labels:
        return []
    mid = width / 2
    condensed = any(x < mid for _, x, _ in labels) and any(x >= mid for _, x, _ in labels) and len(labels) > 1
    if not condensed:
        page_no = labels[0][0]
        return _region_lines(words, page_no, pdf_page, (-1e9, 1e9, labels[0][2] + _LINE_TOLERANCE, 1e9))
    out: list[TLine] = []
    for x_min, x_max in ((-1e9, mid), (mid, 1e9)):
        column = sorted((lab for lab in labels if x_min <= lab[1] < x_max), key=lambda lab: lab[2])
        for i, (page_no, _, y) in enumerate(column):
            y_end = column[i + 1][2] - _LINE_TOLERANCE if i + 1 < len(column) else 1e9
            out += _region_lines(words, page_no, pdf_page, (x_min, x_max, y + _LINE_TOLERANCE, y_end))
    return out


def parse_transcript(path: Path) -> list[TLine]:
    """Every numbered transcript line in a PDF, or [] if it does not look like a transcript."""
    import pymupdf as fitz

    lines: list[TLine] = []
    with fitz.open(path) as pdf:
        for i, page in enumerate(pdf, start=1):
            lines += parse_page(page.get_text("words"), page.rect.width, page.rect.height, i)
    if len({ln.page for ln in lines}) < MIN_PAGES:
        return []
    return lines


# ----------------------------------------------------------------- storing
def store_transcript(conn: sqlite3.Connection, document_id: int, lines: list[TLine]) -> None:
    """Replace the document's transcript lines, and make the searchable text of those PDF pages the
    clean line text, so search snippets do not show the margin numbers ("moved me to 3 the night
    shift"). Covers and the word index keep their original text. Does not commit."""
    conn.execute("DELETE FROM transcript_lines WHERE document_id = ?", (document_id,))
    conn.executemany("INSERT INTO transcript_lines (document_id, pdf_page, page, line, text) VALUES (?, ?, ?, ?, ?)",
                     [(document_id, ln.pdf_page, ln.page, ln.line, ln.text) for ln in lines])
    by_pdf_page: dict[int, list[str]] = {}
    for ln in lines:
        if ln.text:
            by_pdf_page.setdefault(ln.pdf_page, []).append(ln.text)
    # The FTS index follows document_pages through its update trigger.
    conn.executemany("UPDATE document_pages SET text = ? WHERE document_id = ? AND page_no = ?",
                     [("\n".join(texts), document_id, page) for page, texts in by_pdf_page.items()])


def is_transcript(conn: sqlite3.Connection, document_id: int) -> bool:
    return conn.execute("SELECT 1 FROM transcript_lines WHERE document_id = ? LIMIT 1", (document_id,)).fetchone() is not None


def pdf_page_for(conn: sqlite3.Connection, document_id: int, page: int) -> int | None:
    row = conn.execute("SELECT MIN(pdf_page) FROM transcript_lines WHERE document_id = ? AND page = ?",
                       (document_id, page)).fetchone()
    return row[0]


def lines_on(conn: sqlite3.Connection, document_id: int, pdf_pages: list[int]) -> list[sqlite3.Row]:
    if not pdf_pages:
        return []
    marks = ", ".join("?" * len(pdf_pages))
    return conn.execute(f"SELECT * FROM transcript_lines WHERE document_id = ? AND pdf_page IN ({marks})"
                        " ORDER BY rowid", (document_id, *pdf_pages)).fetchall()


# ------------------------------------------------------------------- cites
def cite(start: tuple[int, int], end: tuple[int, int]) -> str:
    """'45:3', '45:3-12' or '45:24-46:2'."""
    if start == end:
        return f"{start[0]}:{start[1]}"
    if start[0] == end[0]:
        return f"{start[0]}:{start[1]}-{end[1]}"
    return f"{start[0]}:{start[1]}-{end[0]}:{end[1]}"


def normalize_quote(text: str) -> str:
    """For matching quotes to page text: rejoin words hyphenated across lines, unify quote marks
    and dashes, collapse whitespace, ignore case."""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-", "­": None, " ": " "}))
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_cite(lines: list, quote: str) -> str | None:
    """The page:line span where `quote` appears in these transcript lines (rows with page, line, text),
    or None. Quotes may run across lines and pages."""
    target = normalize_quote(quote)
    if not target:
        return None
    joined, spans = "", []
    for ln in lines:
        text = normalize_quote(ln["text"])
        if not text:
            continue
        start = len(joined) + (1 if joined else 0)
        joined = f"{joined} {text}" if joined else text
        spans.append((start, len(joined), (ln["page"], ln["line"])))
    at = joined.find(target)
    if at < 0:
        return None
    end = at + len(target) - 1
    first = next(s[2] for s in spans if s[1] > at)
    last = next(s[2] for s in spans if s[1] > end)
    return cite(first, last)


def line_for_terms(lines: list, terms: list[str]) -> tuple[int, int] | None:
    """The first (page, line) whose text contains one of the search words (prefix match, so the
    stemmed search hit "complained" is found for "complain")."""
    stems = [t.casefold()[:max(4, len(t) - 2)] for t in terms if t]
    for ln in lines:
        words = re.findall(r"\w+", ln["text"].casefold())
        if any(w.startswith(s) for s in stems for w in words):
            return ln["page"], ln["line"]
    return None
