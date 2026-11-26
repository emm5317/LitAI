-- 006: deposition transcripts. Each numbered line of a transcript PDF, so cites can read 45:3-12.
-- pdf_page is the page of the file (a condensed transcript has four transcript pages per sheet).
CREATE TABLE transcript_lines (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    pdf_page    INTEGER NOT NULL,
    page        INTEGER NOT NULL,
    line        INTEGER NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX idx_transcript_lines_cite ON transcript_lines(document_id, page, line);
CREATE INDEX idx_transcript_lines_pdf ON transcript_lines(document_id, pdf_page);
