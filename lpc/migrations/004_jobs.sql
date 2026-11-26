-- 004: background jobs (large uploads, importing a folder, rescanning a matter folder).
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('ingest_files', 'import_folder', 'rescan_matter')),
    matter_id   INTEGER REFERENCES matters(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,                  -- shown to the user: "Import folder Client docs"
    params      TEXT NOT NULL DEFAULT '{}',     -- JSON
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    total       INTEGER,                        -- files to process, once known
    done_count  INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,                           -- "212 added, 14 already in the matter"
    problems    TEXT,                           -- JSON list of "file: reason"
    error       TEXT,                           -- why the whole job failed
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, id);
CREATE INDEX idx_jobs_matter ON jobs(matter_id, id);
