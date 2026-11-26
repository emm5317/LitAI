-- 005: AI-suggested chronology events, reviewed before they count.
-- A suggestion is stored 'proposed' with the exact quote it rests on and a link to the logged
-- model call; accepting it gives it a normal E- code. Nothing is accepted automatically.
ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('proposed', 'accepted'));
ALTER TABLE events ADD COLUMN origin TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user', 'ai', 'import'));
ALTER TABLE events ADD COLUMN quote TEXT;
ALTER TABLE events ADD COLUMN ai_call_id INTEGER REFERENCES ai_calls(id) ON DELETE SET NULL;

-- jobs.kind gains 'suggest_events'. SQLite cannot change a CHECK constraint, so rebuild the table.
CREATE TABLE jobs_new (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('ingest_files', 'import_folder', 'rescan_matter', 'suggest_events')),
    matter_id   INTEGER REFERENCES matters(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    total       INTEGER,
    done_count  INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,
    problems    TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at  TEXT,
    finished_at TEXT
);
INSERT INTO jobs_new SELECT * FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX idx_jobs_status ON jobs(status, id);
CREATE INDEX idx_jobs_matter ON jobs(matter_id, id);
