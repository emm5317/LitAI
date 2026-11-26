"""AI-suggested chronology events.

The document's page text goes to the model (through gateway.complete, so consent, the
zero-data-retention model check and the ai_calls log all apply) with a request for dated
events, each backed by a short exact quote and the page it comes from. Before anything is
saved, every quote is looked up in the page text; a suggestion whose quote is not on that page
(or, failing that, on another page sent in the same request) is dropped. Survivors are stored
as *proposed* events (codes P-001...) for the user to accept, edit or reject.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable

from ..chronology import SIGNIFICANCE, EventError, parse_event_date
from ..matters import log_change, normalize_name
from ..transcripts import lines_on, normalize_quote, quote_cite
from .gateway import AIRequest, complete

PROMPT_VERSION = "1"
CHUNK_CHARS = 60_000            # about 15k tokens of page text per request
MAX_PAGES = 500                 # longer documents: the first 500 pages, and the summary says so

SYSTEM = """You build litigation chronologies from documents for an employment lawyer. Read the pages \
and list the events a lawyer would put on a case timeline: who did what, and when.

Rules:
- Use only what the pages say. Do not infer events the pages do not state.
- For each event, copy a short exact quote (5 to 40 words) from the page that supports it, character \
for character, so it can be found by search. Do not fix typos, change punctuation or join text from \
different places inside a quote.
- "page" is the number from the [Page N] marker above the quoted text.
- "date": YYYY-MM-DD, YYYY-MM or YYYY, only as precise as the page supports; null if the page gives no \
date for the event. Set "approximate" to true when the page states the date loosely ("early March", \
"around 2019").
- "description": one plain past-tense sentence in your own words that names who acted.
- "people": names of the people or organizations involved, as the page writes them.
- "significance": "key" for events central to a claim or defense, "background" for context, \
otherwise "supporting".

Reply with only a JSON array and no other text:
[{"date": "2026-03-03", "approximate": false, "description": "...", "people": ["..."], "page": 2, \
"quote": "...", "significance": "supporting"}]
Reply [] if the pages describe no events."""


def chunks(pages: list[tuple[int, str]], limit: int = CHUNK_CHARS) -> list[list[tuple[int, str]]]:
    """Group consecutive pages into requests of at most `limit` characters (a longer page goes alone)."""
    out: list[list[tuple[int, str]]] = []
    size = 0
    for no, text in pages:
        if not out or size + len(text) > limit:
            out.append([])
            size = 0
        out[-1].append((no, text))
        size += len(text)
    return out


def parse_reply(text: str) -> list[dict]:
    """The JSON array in the model's reply (tolerates a code fence or a sentence around it)."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("the reply did not contain a JSON list")
    items = json.loads(text[start:end + 1])
    if not isinstance(items, list):
        raise ValueError("the reply was not a JSON list")
    return [i for i in items if isinstance(i, dict)]


def _match_people(names: list, parties: list[sqlite3.Row]) -> tuple[list[int], list[str]]:
    """Map names to the matter's parties: exact match after normalizing, or every word of the name
    appearing in exactly one party's name ("Doe" -> "Jane Doe" only if there is one Doe)."""
    ids, unmatched = [], []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        norm = normalize_name(name)
        words = set(norm.split())
        exact = [p["id"] for p in parties if normalize_name(p["name"]) == norm]
        loose = [p["id"] for p in parties if words and words <= set(normalize_name(p["name"]).split())]
        found = exact or (loose if len(loose) == 1 else [])
        if found:
            ids.append(found[0])
        else:
            unmatched.append(name.strip())
    return ids, unmatched


def suggest_events(conn: sqlite3.Connection, document_id: int, *, client=None,
                   progress: Callable[[int, int], None] | None = None) -> tuple[str, list[str]]:
    """Ask the model for events in one document and store the verified ones as proposed.

    Returns (summary, problems). Raises gateway.GatewayBlocked when policy forbids the call
    (no consent, no API key) before anything is sent.
    """
    doc = conn.execute("SELECT d.*, m.caption FROM documents d JOIN matters m ON m.id = d.matter_id"
                       " WHERE d.id = ?", (document_id,)).fetchone()
    if doc is None:
        raise ValueError("the document no longer exists")
    pages = [(r["page_no"], r["text"]) for r in conn.execute(
        "SELECT page_no, text FROM document_pages WHERE document_id = ? ORDER BY page_no", (document_id,))]
    if not pages:
        raise ValueError("this document has no text to read (scanned or unreadable)")
    skipped_pages = max(0, len(pages) - MAX_PAGES)
    pages = pages[:MAX_PAGES]
    parties = conn.execute("SELECT mp.id, p.name FROM matter_parties mp JOIN parties p ON p.id = mp.party_id"
                           " WHERE mp.matter_id = ?", (doc["matter_id"],)).fetchall()
    known = {normalize_quote(r["quote"]) for r in conn.execute(
        "SELECT quote FROM events WHERE source_document_id = ? AND quote IS NOT NULL", (document_id,))}
    # A transcript's page text has the margin line numbers mixed in ("...resources on 3 March 3..."),
    # which would break any quote running across two lines. Use the transcript's own line text.
    by_pdf_page: dict[int, list[str]] = {}
    for r in conn.execute("SELECT pdf_page, text FROM transcript_lines WHERE document_id = ? ORDER BY rowid",
                          (document_id,)):
        by_pdf_page.setdefault(r["pdf_page"], []).append(r["text"])
    transcript = bool(by_pdf_page)
    if transcript:
        pages = [(no, "\n".join(by_pdf_page[no]) if no in by_pdf_page else text) for no, text in pages]
    groups = chunks(pages)
    proposed = dropped = repeats = 0
    problems: list[str] = []
    for n, group in enumerate(groups, start=1):
        body = "\n\n".join(f"[Page {no}]\n{text}" for no, text in group)
        names = ", ".join(p["name"] for p in parties) or "(none recorded)"
        prompt = (f"Matter: {doc['caption']}\nPeople in this matter: {names}\n"
                  f"Document: {doc['title']} (document date: {doc['doc_date'] or 'unknown'})\n\n"
                  f"<document>\n{body}\n</document>")
        req = AIRequest(purpose="chronology", prompt=prompt, system=SYSTEM, matter_id=doc["matter_id"],
                        document_ids=[document_id], template="suggest_events", template_version=PROMPT_VERSION,
                        effort="medium")
        reply = complete(conn, req, client=client)
        span = f"pages {group[0][0]}-{group[-1][0]}"
        try:
            items = parse_reply(reply)
        except ValueError as exc:
            problems.append(f"{span}: could not read the model's reply ({exc})"
                            + (" - it was cut off; the pages may be too dense" if req.stop_reason == "max_tokens" else ""))
            items = []
        texts = {no: normalize_quote(text) for no, text in group}
        for item in items:
            quote = str(item.get("quote") or "").strip()
            q = normalize_quote(quote)
            page = item.get("page") if isinstance(item.get("page"), int) else None
            if len(q) < 8 or not str(item.get("description") or "").strip():
                dropped += 1
                continue
            if page not in texts or q not in texts[page]:
                page = next((no for no, t in texts.items() if q in t), None)
            if page is None:
                dropped += 1
                continue
            if q in known:
                repeats += 1
                continue
            known.add(q)
            pin = f"p. {page}"
            if transcript:     # a transcript: cite page:line (the quote may run onto the next sheet)
                pin = quote_cite(lines_on(conn, document_id, [page, page + 1]), quote) or pin
            try:
                date, precision = parse_event_date(str(item.get("date") or ""), bool(item.get("approximate")))
            except EventError:
                date, precision = None, "unknown"
            people, unmatched = _match_people(item.get("people") or [], parties)
            significance = item.get("significance") if item.get("significance") in SIGNIFICANCE else "supporting"
            code_row = conn.execute("SELECT MAX(CAST(substr(event_code, 3) AS INTEGER)) FROM events"
                                    " WHERE matter_id = ? AND event_code LIKE 'P-%'", (doc["matter_id"],)).fetchone()
            cur = conn.execute(
                "INSERT INTO events (matter_id, event_code, event_date, date_precision, description, significance,"
                " source_document_id, pin, notes, status, origin, quote, ai_call_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', 'ai', ?, ?)",
                (doc["matter_id"], f"P-{(code_row[0] or 0) + 1:03d}", date, precision,
                 str(item["description"]).strip()[:1000], significance, document_id, pin,
                 f"Also named: {', '.join(unmatched)}" if unmatched else None, quote, req.call_id))
            conn.executemany("INSERT OR IGNORE INTO event_people (event_id, matter_party_id) VALUES (?, ?)",
                             [(cur.lastrowid, p) for p in dict.fromkeys(people)])
            proposed += 1
        conn.commit()
        if progress:
            progress(n, len(groups))
    log_change(conn, doc["matter_id"], "events", None,
               f"AI proposed {proposed} event{'s' if proposed != 1 else ''} from {doc['title']}", actor="ai")
    conn.commit()
    parts = [f"{proposed} event{'s' if proposed != 1 else ''} proposed for review"]
    if dropped:
        parts.append(f"{dropped} dropped because the quote was not found on the page")
    if repeats:
        parts.append(f"{repeats} already suggested")
    if skipped_pages:
        parts.append(f"only the first {MAX_PAGES} pages were read ({skipped_pages} not sent)")
    return ", ".join(parts), problems
