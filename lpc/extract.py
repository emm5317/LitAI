"""Turn a file into page text (and, for email, sender, recipients, date and attachments).

    PDF   - PyMuPDF, one entry per page.
    DOCX  - read word/document.xml directly (no Word needed). Pages follow the page breaks Word
            recorded when the file was last saved, so "p. 3" matches what you saw in Word.
    EML   - Python's email package: headers + body as one page; attachments returned separately.
    MSG   - Outlook messages via extract-msg, same shape as EML.
    TXT, MD, CSV - one page.

Anything else returns no pages (text_status 'not_applicable'). A file that cannot be read
raises ExtractError; ingest records it as 'failed' and moves on.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

TEXT_SUFFIXES = {".txt", ".md", ".csv"}
EMAIL_SUFFIXES = {".eml", ".msg"}
SUPPORTED_SUFFIXES = {".pdf", ".docx"} | TEXT_SUFFIXES | EMAIL_SUFFIXES


class ExtractError(Exception):
    """The file could not be read (corrupt, password-protected, not what its name says)."""


@dataclass
class Extracted:
    pages: list[str]
    title: str | None = None
    author: str | None = None
    recipients: str | None = None
    doc_date: str | None = None                       # YYYY-MM-DD
    attachments: list[tuple[str, bytes]] = field(default_factory=list)   # (file name, contents)


def extract(path: Path) -> Extracted:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix == ".docx":
            return Extracted(pages=docx_pages(path.read_bytes()))
        if suffix == ".eml":
            return _eml(path.read_bytes())
        if suffix == ".msg":
            return _msg(path)
        if suffix in TEXT_SUFFIXES:
            return Extracted(pages=[path.read_text(encoding="utf-8", errors="replace")])
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(f"{type(exc).__name__}: {exc}") from exc
    return Extracted(pages=[])


# --------------------------------------------------------------------- PDF
def _pdf(path: Path) -> Extracted:
    import pymupdf as fitz

    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ExtractError("the PDF is password-protected")
        return Extracted(pages=[page.get_text("text") for page in pdf])


# -------------------------------------------------------------------- DOCX
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_pages(data: bytes) -> list[str]:
    """Text of a .docx, split into pages.

    Word writes a <w:lastRenderedPageBreak/> wherever a page ended when the file was last saved;
    if those exist they give the real page numbers. Files made by other programs lack them, so
    fall back to hard page breaks. Deleted tracked changes and field codes are left out.
    """
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")
    if body is None:
        return []
    rendered = any(True for _ in body.iter(f"{_W}lastRenderedPageBreak"))
    pages: list[list[str]] = [[]]

    def walk(el) -> None:
        for child in el:
            tag = child.tag
            if tag == f"{_W}t":
                pages[-1].append(child.text or "")
            elif tag == f"{_W}tab":
                pages[-1].append("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):
                if child.get(f"{_W}type") == "page" and not rendered:
                    pages.append([])
                else:
                    pages[-1].append("\n")
            elif tag == f"{_W}lastRenderedPageBreak":
                pages.append([])
            elif tag == f"{_W}p":
                walk(child)
                pages[-1].append("\n")
            elif tag == f"{_W}tc":
                walk(child)
                pages[-1].append("\t")
            elif tag.endswith("}Fallback"):        # duplicate copy of text-box content
                continue
            else:
                walk(child)

    walk(body)
    texts = [re.sub(r"\n{3,}", "\n\n", "".join(p)).strip() for p in pages]
    while len(texts) > 1 and not texts[-1]:        # a break at the very end makes an empty page
        texts.pop()
    return texts


# ------------------------------------------------------------------- email
class _TextOnly(HTMLParser):
    """Strip tags from an HTML email body, keeping line breaks roughly where they were."""

    BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextOnly()
    parser.feed(markup)
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _email_page(subject, sender, to, cc, date, body) -> str:
    lines = [f"From: {sender or ''}", f"To: {to or ''}"]
    if cc:
        lines.append(f"Cc: {cc}")
    lines += [f"Date: {date or ''}", f"Subject: {subject or ''}", "", body.strip()]
    return "\n".join(lines)


def _iso_date(value) -> str | None:
    if not value:
        return None
    if hasattr(value, "date"):
        return value.date().isoformat()
    try:
        return email.utils.parsedate_to_datetime(str(value)).date().isoformat()
    except (TypeError, ValueError):
        return None


def _eml(data: bytes) -> Extracted:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = html_to_text(body)
    attachments = []
    for att in msg.iter_attachments():
        ctype = att.get_content_type()
        # Signature logos and other inline images are not real attachments.
        if ctype.startswith("image/") and (att.get("Content-ID") or att.get_content_disposition() == "inline"):
            continue
        if ctype == "message/rfc822":
            inner = att.get_payload(0)
            name = att.get_filename() or f"{inner.get('subject') or 'attached message'}.eml"
            attachments.append((name, inner.as_bytes()))
            continue
        payload = att.get_payload(decode=True)
        if payload:
            attachments.append((att.get_filename() or "attachment", payload))
    subject = str(msg.get("subject", "") or "")
    return Extracted(
        pages=[_email_page(subject, msg.get("from"), msg.get("to"), msg.get("cc"), msg.get("date"), body)],
        title=subject or None, author=str(msg.get("from") or "") or None,
        recipients=", ".join(str(v) for v in (msg.get("to"), msg.get("cc")) if v) or None,
        doc_date=_iso_date(msg.get("date")), attachments=attachments,
    )


def _msg(path: Path) -> Extracted:
    import extract_msg

    msg = extract_msg.openMsg(str(path))
    try:
        body = msg.body or ""
        if not body.strip() and getattr(msg, "htmlBody", None):
            raw = msg.htmlBody
            body = html_to_text(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
        attachments = []
        for att in msg.attachments:
            data = att.data
            name = att.longFilename or att.shortFilename or getattr(att, "name", None) or "attachment"
            if hasattr(data, "exportBytes"):                 # an Outlook message attached to this one
                attachments.append((f"{getattr(data, 'subject', None) or 'attached message'}.msg", data.exportBytes()))
            elif isinstance(data, bytes) and data:
                if getattr(att, "hidden", False):            # inline signature images
                    continue
                attachments.append((name, data))
        return Extracted(
            pages=[_email_page(msg.subject, msg.sender, msg.to, msg.cc, msg.date, body)],
            title=msg.subject or None, author=msg.sender or None,
            recipients=", ".join(v for v in (msg.to, msg.cc) if v) or None,
            doc_date=_iso_date(msg.date), attachments=attachments,
        )
    finally:
        msg.close()
