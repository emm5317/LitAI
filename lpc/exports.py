"""Chronology exports: a Word table, a per-witness depo outline (Word), and CSV / Excel.

Each function returns the file's bytes; the app saves them into the matter's
"20 Work Product" folder as "[SHORT NAME] - YYYY.MM.DD - [DESC].ext" and opens them.
Word stays the editor: these are starting points, not finished documents.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3

from .chronology import format_event_date
from .matters import folder_name

WORK_PRODUCT = "Attorney work product - privileged and confidential"
COLUMNS = ("Code", "Date", "Precision", "What happened", "Significance", "Disputed", "People", "Issues",
           "Source document", "Pin", "Bates", "Notes")


def export_name(short_name: str, description: str, suffix: str, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return folder_name(f"{short_name} - {today:%Y.%m.%d} - {description}") + suffix


def _source(e: sqlite3.Row) -> str:
    parts = [e["source_title"] or "", e["pin"] or ""]
    text = ", ".join(p for p in parts if p)
    if e["source_bates"]:
        text += f" ({e['source_bates']})"
    return text


def _rows(events: list[sqlite3.Row]) -> list[list]:
    return [[e["event_code"], e["event_date"] or "", e["date_precision"], e["description"], e["significance"],
             "yes" if e["disputed"] else "", e["people"] or "", e["issue_tags"] or "", e["source_title"] or "",
             e["pin"] or "", e["source_bates"] or "", e["notes"] or ""] for e in events]


# -------------------------------------------------------------- CSV / Excel
def chronology_csv(events: list[sqlite3.Row]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    writer.writerows(_rows(events))
    return buf.getvalue().encode("utf-8-sig")        # the BOM makes Excel read accents correctly


def chronology_xlsx(matter: sqlite3.Row, events: list[sqlite3.Row]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Chronology"
    ws.append(COLUMNS)
    for row in _rows(events):
        ws.append(row)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    widths = {"A": 8, "B": 12, "C": 11, "D": 60, "E": 12, "F": 9, "G": 24, "H": 22, "I": 30, "J": 12, "K": 16, "L": 30}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.properties.title = f"Chronology - {matter['caption']}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- Word
def _new_document(landscape: bool = False):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches, Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    section = doc.sections[0]
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = section.page_height, section.page_width
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Inches(0.8))
    section.header.paragraphs[0].text = WORK_PRODUCT
    return doc


def _repeat_header_row(row) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    flag = OxmlElement("w:tblHeader")
    flag.set(qn("w:val"), "true")
    row._tr.get_or_add_trPr().append(flag)


def _to_bytes(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def chronology_docx(matter: sqlite3.Row, events: list[sqlite3.Row], subtitle: str = "") -> bytes:
    from docx.shared import Inches

    doc = _new_document(landscape=True)
    doc.add_heading(f"Chronology: {matter['caption']}", level=1)
    note = f"Prepared {dt.date.today():%B %d, %Y}. {len(events)} event{'s' if len(events) != 1 else ''}."
    doc.add_paragraph(f"{note} {subtitle}".strip())
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, title in zip(table.rows[0].cells, ("Code", "Date", "What happened", "People", "Source")):
        cell.text = title
        cell.paragraphs[0].runs[0].bold = True
    _repeat_header_row(table.rows[0])
    for e in events:
        cells = table.add_row().cells
        cells[0].text = e["event_code"]
        cells[1].text = format_event_date(e["event_date"], e["date_precision"])
        what = cells[2].paragraphs[0]
        what.add_run(e["description"]).bold = e["significance"] == "key"
        if e["disputed"]:
            what.add_run("  [disputed]").italic = True
        if e["notes"]:
            cells[2].add_paragraph(e["notes"]).runs[0].italic = True
        cells[3].text = e["people"] or ""
        cells[4].text = _source(e)
    for row in table.rows:
        for cell, width in zip(row.cells, (0.7, 1.1, 4.6, 1.6, 1.9)):
            cell.width = Inches(width)
    return _to_bytes(doc)


def witness_outline_docx(matter: sqlite3.Row, witness: sqlite3.Row, events: list[sqlite3.Row]) -> bytes:
    """A depo outline skeleton for one person: their events in date order, room for questions under
    each, and the exhibits to show them (each source document once, in the order first used)."""
    doc = _new_document()
    doc.add_heading(f"Deposition outline: {witness['name']}", level=1)
    role = witness["role"].replace("_", " ")
    doc.add_paragraph(f"{matter['caption']}. {role.capitalize()}" + (f", {witness['title']}" if witness["title"] else "")
                      + f". Prepared {dt.date.today():%B %d, %Y}.")
    exhibits: dict[int, sqlite3.Row] = {}
    doc.add_heading("Events", level=2)
    if not events:
        doc.add_paragraph("No chronology events are tied to this person yet.")
    for e in events:
        doc.add_heading(f"{e['event_code']}  {format_event_date(e['event_date'], e['date_precision'])}", level=3)
        p = doc.add_paragraph(e["description"])
        if e["disputed"]:
            p.add_run("  [disputed]").italic = True
        if e["source_document_id"]:
            exhibits.setdefault(e["source_document_id"], e)
            doc.add_paragraph(f"Source: {_source(e)}").runs[0].italic = True
        doc.add_paragraph("Questions:")
        doc.add_paragraph("", style="List Bullet")
    doc.add_heading("Exhibits to use", level=2)
    if not exhibits:
        doc.add_paragraph("None yet.")
    for e in exhibits.values():
        text = e["source_title"] + (f" ({e['source_bates']})" if e["source_bates"] else "")
        doc.add_paragraph(text, style="List Number")
    return _to_bytes(doc)
