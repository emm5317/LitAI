"""Chronology: dated events in a matter, each tied to people, issues and a source document + pin.

Event dates may be partial, because records often are: "2026-03-03" (day), "2026-03" (month),
"2026" (year), or blank (undated). Partial dates sort sensibly as text (2026 < 2026-03 <
2026-03-03). Each event gets a stable code (E-001, E-002...) that depo outlines can refer to.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3

from .matters import log_change

SIGNIFICANCE = ("key", "supporting", "background")
EVENT_FIELDS = ("event_date", "date_precision", "description", "significance", "disputed", "source_document_id",
                "pin", "issue_tags", "notes")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class EventError(ValueError):
    """A problem the user can fix; the message says what to do."""


# ------------------------------------------------------------------ dates
def parse_event_date(text: str, approximate: bool = False) -> tuple[str | None, str]:
    """Read a typed date into (stored value, precision).

    Accepts 2026-03-03, 2026-03, 2026, 3/3/2026 and 3/2026. Blank means undated.
    """
    t = (text or "").strip()
    if not t:
        return None, "unknown"
    patterns = (
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda y, m, d: (y, m, d)),
        (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda m, d, y: (y, m, d)),
        (r"(\d{4})-(\d{1,2})", lambda y, m: (y, m, None)),
        (r"(\d{1,2})/(\d{4})", lambda m, y: (y, m, None)),
        (r"(\d{4})", lambda y: (y, None, None)),
    )
    for pattern, order in patterns:
        match = re.fullmatch(pattern, t)
        if not match:
            continue
        year, month, day = (int(v) if v else None for v in order(*match.groups()))
        try:
            dt.date(year, month or 1, day or 1)
        except ValueError:
            break
        if day:
            value, precision = f"{year:04d}-{month:02d}-{day:02d}", "day"
        elif month:
            value, precision = f"{year:04d}-{month:02d}", "month"
        else:
            value, precision = f"{year:04d}", "year"
        return value, "approximate" if approximate else precision
    raise EventError(f"Could not read the date {t!r}. Use 2026-03-03, 2026-03, 2026 or 3/3/2026.")


def format_event_date(value: str | None, precision: str | None = None) -> str:
    """'Mar 3, 2026', 'Mar 2026', '2026', 'c. Mar 2026', or 'Undated'."""
    if not value:
        return "Undated"
    parts = value.split("-")
    if len(parts) == 3:
        text = f"{_MONTHS[int(parts[1]) - 1]} {int(parts[2])}, {parts[0]}"
    elif len(parts) == 2:
        text = f"{_MONTHS[int(parts[1]) - 1]} {parts[0]}"
    else:
        text = parts[0]
    return f"c. {text}" if precision == "approximate" else text


def pin_page(pin: str | None) -> int | None:
    """The page number a pin cite points to ('p. 17' -> 17, '45:3-12' -> 45)."""
    match = re.search(r"\d+", pin or "")
    return int(match.group()) if match else None


def clean_tags(text: str | None) -> str | None:
    tags = [t.strip() for t in (text or "").split(",") if t.strip()]
    return ", ".join(dict.fromkeys(tags)) or None          # drop duplicates, keep order


# ----------------------------------------------------------------- saving
def next_event_code(conn: sqlite3.Connection, matter_id: int, prefix: str = "E") -> str:
    row = conn.execute(
        "SELECT MAX(CAST(substr(event_code, 3) AS INTEGER)) FROM events WHERE matter_id = ? AND event_code LIKE ?",
        (matter_id, f"{prefix}-%")).fetchone()
    return f"{prefix}-{(row[0] or 0) + 1:03d}"


def save_event(conn: sqlite3.Connection, matter_id: int, data: dict, people: list[int],
               event_id: int | None = None, actor: str = "user") -> int:
    """Insert (event_id None) or update an event and its people. Does not commit.

    `data` keys follow EVENT_FIELDS; `people` are matter_parties ids. Documents and people
    from other matters are refused.
    """
    values = {f: data.get(f).strip() if isinstance(data.get(f), str) else data.get(f) for f in EVENT_FIELDS}
    values = {f: (None if v in ("", None) else v) for f, v in values.items()}
    if not values["description"]:
        raise EventError("Describe what happened.")
    values["significance"] = values["significance"] or "supporting"
    values["disputed"] = 1 if values["disputed"] else 0
    values["date_precision"] = values["date_precision"] or ("unknown" if not values["event_date"] else "day")
    values["issue_tags"] = clean_tags(values["issue_tags"])
    doc = values["source_document_id"]
    if doc is not None and not conn.execute("SELECT 1 FROM documents WHERE id = ? AND matter_id = ?",
                                            (doc, matter_id)).fetchone():
        raise EventError("That source document is not in this matter.")
    valid = {r[0] for r in conn.execute("SELECT id FROM matter_parties WHERE matter_id = ?", (matter_id,))}
    people = [p for p in dict.fromkeys(people) if p in valid]

    if event_id is None:
        code = next_event_code(conn, matter_id)
        cols = ", ".join(values)
        cur = conn.execute(f"INSERT INTO events (matter_id, event_code, {cols}) VALUES (?, ?, {', '.join('?' * len(values))})",
                           (matter_id, code, *values.values()))
        event_id = cur.lastrowid
        summary = f"Added event {code}: {values['description'][:80]}"
    else:
        code = conn.execute("SELECT event_code FROM events WHERE id = ?", (event_id,)).fetchone()[0]
        conn.execute(f"UPDATE events SET {', '.join(f'{f} = ?' for f in values)} WHERE id = ?",
                     (*values.values(), event_id))
        conn.execute("DELETE FROM event_people WHERE event_id = ?", (event_id,))
        summary = f"Edited event {code}"
    conn.executemany("INSERT INTO event_people (event_id, matter_party_id) VALUES (?, ?)",
                     [(event_id, p) for p in people])
    log_change(conn, matter_id, "events", event_id, summary, actor=actor)
    return event_id


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    row = conn.execute("SELECT matter_id, event_code FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    log_change(conn, row["matter_id"], "events", event_id, f"Deleted event {row['event_code']}")


# ---------------------------------------------------------------- reading
def chronology(conn: sqlite3.Connection, matter_id: int, *, person: int | None = None, issue: str | None = None,
               key_only: bool = False, disputed_only: bool = False, status: str = "accepted") -> list[sqlite3.Row]:
    """Events in date order (undated last), with people names and the source document.
    AI suggestions awaiting review are left out unless status='proposed'."""
    sql = (
        "SELECT e.*, d.title AS source_title, d.file_path AS source_path, d.bates_begin AS source_bates,"
        " (SELECT group_concat(p.name, ', ') FROM event_people ep"
        "   JOIN matter_parties mp ON mp.id = ep.matter_party_id JOIN parties p ON p.id = mp.party_id"
        "   WHERE ep.event_id = e.id) AS people"
        " FROM events e LEFT JOIN documents d ON d.id = e.source_document_id WHERE e.matter_id = ? AND e.status = ?"
    )
    params: list = [matter_id, status]
    if person:
        sql += " AND EXISTS (SELECT 1 FROM event_people ep WHERE ep.event_id = e.id AND ep.matter_party_id = ?)"
        params.append(person)
    if issue:
        sql += " AND instr(', ' || lower(e.issue_tags) || ',', ', ' || lower(?) || ',') > 0"
        params.append(issue)
    if key_only:
        sql += " AND e.significance = 'key'"
    if disputed_only:
        sql += " AND e.disputed = 1"
    sql += " ORDER BY e.event_date IS NULL, e.event_date, e.event_code"
    return conn.execute(sql, params).fetchall()


def accept_event(conn: sqlite3.Connection, event_id: int) -> str:
    """Accept an AI suggestion: it joins the chronology with the next E- code. Does not commit."""
    row = conn.execute("SELECT matter_id, event_code, status FROM events WHERE id = ?", (event_id,)).fetchone()
    if row["status"] != "proposed":
        raise EventError(f"{row['event_code']} is already in the chronology.")
    code = next_event_code(conn, row["matter_id"])
    conn.execute("UPDATE events SET status = 'accepted', event_code = ? WHERE id = ?", (code, event_id))
    log_change(conn, row["matter_id"], "events", event_id, f"Accepted AI-suggested event {code} (was {row['event_code']})")
    return code


def reject_event(conn: sqlite3.Connection, event_id: int) -> None:
    row = conn.execute("SELECT matter_id, event_code, status FROM events WHERE id = ?", (event_id,)).fetchone()
    if row["status"] != "proposed":
        raise EventError(f"{row['event_code']} is in the chronology; edit or delete it instead.")
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    log_change(conn, row["matter_id"], "events", event_id, f"Rejected AI-suggested event {row['event_code']}")


def event_people_ids(conn: sqlite3.Connection, event_id: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT matter_party_id FROM event_people WHERE event_id = ?", (event_id,))}


def issue_tags(conn: sqlite3.Connection, matter_id: int) -> list[str]:
    tags: dict[str, None] = {}
    for (text,) in conn.execute("SELECT issue_tags FROM events WHERE matter_id = ? AND issue_tags IS NOT NULL",
                                (matter_id,)):
        for t in text.split(","):
            tags.setdefault(t.strip(), None)
    return sorted(tags, key=str.lower)
