"""Key Events workbook: a blank template to fill in, and an import into a matter's chronology.

Template columns (sheet "Key Events"):
    Date | Approx. | Event | Significance | Disputed | People | Issues | Source | Pin | Notes

The import also understands common variations of those headers (What happened, Witnesses,
Cite, Bates, Comments...) and this app's own Excel/CSV chronology export, so a chronology can
move between matters. Each row is read, checked and shown in a preview before anything is saved;
rows whose Event starts with "Example:" (the template's sample row) are skipped.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .chronology import EventError, clean_tags, parse_event_date, save_event
from .matters import add_party_to_matter, log_change, normalize_name

TEMPLATE_COLUMNS = ("Date", "Approx.", "Event", "Significance", "Disputed", "People", "Issues", "Source", "Pin", "Notes")

# Header text (lowercase, punctuation removed) -> field. Includes this app's export headers.
HEADERS = {
    "date": "date", "event date": "date", "when": "date", "date of event": "date",
    "approx": "approximate", "approximate": "approximate", "circa": "approximate", "precision": "approximate",
    "date approximate": "approximate",
    "event": "description", "description": "description", "what happened": "description", "fact": "description",
    "facts": "description", "event description": "description", "summary": "description",
    "significance": "significance", "importance": "significance", "priority": "significance",
    "key": "significance", "key event": "significance",
    "disputed": "disputed", "contested": "disputed", "dispute": "disputed", "in dispute": "disputed",
    "people": "people", "persons": "people", "witnesses": "people", "witness": "people", "who": "people",
    "actors": "people", "people involved": "people", "individuals": "people",
    "issues": "issues", "issue": "issues", "tags": "issues", "issue tags": "issues", "claims": "issues",
    "topics": "issues",
    "source": "source", "source document": "source", "document": "source", "doc": "source", "exhibit": "source",
    "document title": "source",
    "bates": "bates", "bates number": "bates", "bates no": "bates",
    "pin": "pin", "pin cite": "pin", "cite": "pin", "citation": "pin", "page": "pin", "pages": "pin",
    "pageline": "pin",
    "notes": "notes", "note": "notes", "comments": "notes", "comment": "notes",
}
FIELD_LABELS = {"date": "Date", "approximate": "Approx.", "description": "Event", "significance": "Significance",
                "disputed": "Disputed", "people": "People", "issues": "Issues", "source": "Source",
                "bates": "Bates", "pin": "Pin", "notes": "Notes"}
_YES = {"y", "yes", "x", "true", "1", "approx", "approximate", "c", "circa", "disputed", "contested"}
_KEY = {"key", "yes", "y", "x", "high", "critical", "hot", "1", "true"}
_BACKGROUND = {"background", "low", "context"}


class WorkbookError(ValueError):
    """The file cannot be imported; the message says why."""


@dataclass
class Row:
    number: int                     # spreadsheet row number, for messages
    data: dict                      # save_event() fields
    people: list[str]
    source_text: str = ""
    problems: list[str] = field(default_factory=list)


@dataclass
class Preview:
    sheet: str
    mapped: dict[str, str]          # header as written -> field
    ignored: list[str]              # headers not imported
    rows: list[Row]
    skipped_examples: int = 0


def _header_key(text) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text or "").lower().replace("/", " ")).strip()


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _read_table(path: Path) -> tuple[str, list[list]]:
    """(sheet name, rows of cell values) from an .xlsx or .csv file."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return path.stem, [row for row in csv.reader(fh)]
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise WorkbookError(f"Could not open the workbook ({type(exc).__name__}).") from exc
        sheet = next((ws for ws in wb.worksheets if ws.title.strip().lower() == "key events"), wb.worksheets[0])
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        wb.close()
        return sheet.title, rows
    raise WorkbookError("Choose an Excel workbook (.xlsx) or a CSV file.")


def _date(value, approximate: bool) -> tuple[str | None, str, str | None]:
    """(stored date, precision, problem) from a cell."""
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return value.isoformat(), "approximate" if approximate else "day", None
    text = _cell_text(value)
    try:
        date, precision = parse_event_date(text, approximate)
        return date, precision, None
    except EventError:
        return None, "unknown", f"Date in workbook: {text}"


def _people(text: str) -> list[str]:
    parts = re.split(r"[;\n]", text) if re.search(r"[;\n]", text) else text.split(",")
    return [p.strip() for p in parts if p.strip()]


def read_workbook(path: Path) -> Preview:
    sheet, table = _read_table(path)
    header_at = next((i for i, row in enumerate(table[:15])
                      if any(HEADERS.get(_header_key(c)) == "description" for c in row)), None)
    if header_at is None:
        raise WorkbookError("Could not find a header row with an Event (or Description / What happened) column "
                            "in the first 15 rows.")
    headers = table[header_at]
    columns: dict[int, str] = {}
    mapped: dict[str, str] = {}
    ignored: list[str] = []
    for i, h in enumerate(headers):
        name = _cell_text(h)
        if not name:
            continue
        key = HEADERS.get(_header_key(name))
        if key and key not in columns.values():
            columns[i] = key
            mapped[name] = key
        else:
            ignored.append(name)
    rows: list[Row] = []
    examples = 0
    for offset, raw in enumerate(table[header_at + 1:], start=header_at + 2):
        cells = {key: (raw[i] if i < len(raw) else None) for i, key in columns.items()}
        text = {key: _cell_text(v) for key, v in cells.items()}
        if not any(text.values()):
            continue
        description = text.get("description", "")
        if description.lower().startswith("example:"):
            examples += 1
            continue
        approx_text = text.get("approximate", "").lower()
        approximate = approx_text in _YES or approx_text.startswith("approx")
        date, precision, date_problem = _date(cells.get("date"), approximate)
        sig = text.get("significance", "").lower()
        if sig in _KEY:
            significance = "key"
        elif sig in _BACKGROUND:
            significance = "background"
        else:
            significance = "supporting"
        pin = text.get("pin", "")
        if pin.isdigit():
            pin = f"p. {pin}"
        notes = [n for n in (text.get("notes", ""), date_problem) if n]
        row = Row(number=offset, people=_people(text.get("people", "")),
                  source_text=text.get("source", "") or text.get("bates", ""),
                  data={"event_date": date, "date_precision": precision, "description": description,
                        "significance": significance, "disputed": 1 if text.get("disputed", "").lower() in _YES else 0,
                        "pin": pin, "issue_tags": clean_tags(text.get("issues", "")), "notes": "\n".join(notes)})
        if not description:
            row.problems.append("no Event text; this row will be skipped")
        if date_problem:
            row.problems.append(f"could not read the date {text.get('date')!r}; kept in Notes")
        rows.append(row)
    return Preview(sheet=sheet, mapped=mapped, ignored=ignored, rows=rows, skipped_examples=examples)


# ------------------------------------------------------------------ import
def match_sources(conn: sqlite3.Connection, matter_id: int, rows: list[Row]) -> dict[str, int]:
    """Source text -> document id, matching title, file name or Bates number (ignoring case)."""
    docs = conn.execute("SELECT id, title, file_path, bates_begin FROM documents WHERE matter_id = ?",
                        (matter_id,)).fetchall()
    lookup: dict[str, int] = {}
    for d in docs:
        for key in (d["title"], Path(d["file_path"].replace("\\", "/")).name, Path(d["file_path"].replace("\\", "/")).stem,
                    d["bates_begin"]):
            if key:
                lookup.setdefault(key.strip().casefold(), d["id"])
    return {r.source_text: lookup[r.source_text.casefold()] for r in rows
            if r.source_text and r.source_text.casefold() in lookup}


def import_rows(conn: sqlite3.Connection, matter_id: int, preview: Preview, *, add_people: bool,
                file_name: str) -> dict:
    """Save the previewed rows as chronology events. Returns counts. Commits once at the end."""
    sources = match_sources(conn, matter_id, preview.rows)
    existing = {(r["event_date"], r["description"].casefold()) for r in
                conn.execute("SELECT event_date, description FROM events WHERE matter_id = ?", (matter_id,))}

    def parties() -> dict[str, int]:
        return {normalize_name(r["name"]): r["id"] for r in conn.execute(
            "SELECT mp.id, p.name FROM matter_parties mp JOIN parties p ON p.id = mp.party_id WHERE mp.matter_id = ?",
            (matter_id,))}

    known = parties()
    counts = {"added": 0, "duplicates": 0, "skipped": 0, "people_added": 0}
    for row in preview.rows:
        data = dict(row.data)
        if not data["description"]:
            counts["skipped"] += 1
            continue
        if (data["event_date"], data["description"].casefold()) in existing:
            counts["duplicates"] += 1
            continue
        notes = [data["notes"]] if data["notes"] else []
        if row.source_text:
            if row.source_text in sources:
                data["source_document_id"] = sources[row.source_text]
            else:
                notes.append(f"Source in workbook: {row.source_text}")
        people_ids, unmatched = [], []
        for name in row.people:
            key = normalize_name(name)
            if key not in known and add_people:
                add_party_to_matter(conn, matter_id, name, "witness", kind="person")
                known = parties()
                counts["people_added"] += 1
            if key in known:
                people_ids.append(known[key])
            else:
                unmatched.append(name)
        if unmatched:
            notes.append(f"Also named: {', '.join(unmatched)}")
        data["notes"] = "\n".join(notes)
        event_id = save_event(conn, matter_id, data, people_ids, actor="system")
        conn.execute("UPDATE events SET origin = 'import' WHERE id = ?", (event_id,))
        existing.add((data["event_date"], data["description"].casefold()))
        counts["added"] += 1
    log_change(conn, matter_id, "events", None,
               f"Imported {counts['added']} event{'s' if counts['added'] != 1 else ''} from {file_name}")
    conn.commit()
    return counts


# ---------------------------------------------------------------- template
def key_events_template() -> bytes:
    """A blank Key Events workbook: the columns above, one example row (skipped on import), and a
    second sheet explaining each column."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Key Events"
    ws.append(TEMPLATE_COLUMNS)
    ws.append(["2026-03-03", "", "Example: Employee e-mails HR complaining about unpaid overtime", "Key", "",
               "Jane Example; HR Manager Example", "overtime, retaliation", "HR complaint email", "p. 1",
               "Delete this row, or leave it: rows starting with Example: are not imported"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for cell in ws[2]:
        cell.font = Font(italic=True, color="888888")
    for col, width in zip("ABCDEFGHIJ", (12, 8, 60, 13, 9, 28, 24, 30, 12, 36)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    for column, choices in (("D", '"Key,Supporting,Background"'), ("B", '"Y"'), ("E", '"Y"')):
        rule = DataValidation(type="list", formula1=choices, allow_blank=True)
        ws.add_data_validation(rule)
        rule.add(f"{column}2:{column}2000")
    guide = wb.create_sheet("How to fill it in")
    guide.append(["Column", "What to put in it"])
    for row in (
        ("Date", "2026-03-03, 2026-03 (month only), 2026 (year only), or 3/3/2026. Blank if undated."),
        ("Approx.", "Y if the date is approximate (\"early March\", \"around 2019\")."),
        ("Event", "What happened, in a sentence. The only required column."),
        ("Significance", "Key, Supporting or Background. Blank means Supporting."),
        ("Disputed", "Y if the other side disputes it."),
        ("People", "Names separated by semicolons. Names not yet in the matter can be added as witnesses."),
        ("Issues", "Tags separated by commas, e.g. overtime, retaliation."),
        ("Source", "The document's title, file name or first Bates number, as it appears in the matter."),
        ("Pin", "p. 4, or 45:3-12 for a transcript."),
        ("Notes", "Anything else."),
    ):
        guide.append(row)
    guide["A1"].font = guide["B1"].font = Font(bold=True)
    guide.column_dimensions["A"].width = 14
    guide.column_dimensions["B"].width = 100
    for row in guide.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
