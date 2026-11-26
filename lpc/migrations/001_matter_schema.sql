-- Law Practice Console - matter schema v1
-- Modeled on the Litigation Case Workbook tabs (Dashboard, Task List, Key Events,
-- People, Document Log, Claims, hidden Lists sheet) and the Case Wiki notes
-- (Issues, Discovery, Changelog). See docs/SCHEMA.md for the tab-to-table map.
--
-- Conventions
--   * Dates are ISO-8601 text (YYYY-MM-DD); timestamps are ISO-8601 UTC text.
--   * Closed vocabularies (the workbook's hidden Lists sheet) are CHECK constraints.
--   * Every matter-scoped table carries matter_id with ON DELETE CASCADE.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Party registry (powers the conflicts check across all matters)
-- ---------------------------------------------------------------------------
CREATE TABLE parties (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,              -- lowercase, punctuation and entity suffixes stripped
    kind            TEXT NOT NULL DEFAULT 'person'
                    CHECK (kind IN ('person', 'organization')),
    email           TEXT,
    phone           TEXT,
    organization    TEXT,                       -- employer/firm for a person
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_parties_normalized ON parties(normalized_name);

CREATE TABLE party_aliases (
    id               INTEGER PRIMARY KEY,
    party_id         INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    alias            TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'aka'
                     CHECK (kind IN ('aka', 'dba', 'former_name', 'parent', 'subsidiary', 'affiliate'))
);
CREATE INDEX idx_party_aliases_normalized ON party_aliases(normalized_alias);

-- ---------------------------------------------------------------------------
-- Matters (workbook: Dashboard; wiki: 00-HUB)
-- ---------------------------------------------------------------------------
CREATE TABLE matters (
    id                INTEGER PRIMARY KEY,
    short_name        TEXT NOT NULL UNIQUE,     -- used in file names: "[SHORT NAME] - YYYY.MM.DD - [DESC]"
    caption           TEXT NOT NULL,            -- e.g. "Doe v. Acme Corp."
    side              TEXT NOT NULL
                      CHECK (side IN ('employer', 'employee')),
    matter_type       TEXT NOT NULL DEFAULT 'single_plaintiff'
                      CHECK (matter_type IN ('single_plaintiff', 'class', 'collective', 'hybrid_class_collective',
                                             'individual_arbitration', 'mass_arbitration', 'pre_suit',
                                             'agency_charge', 'counseling', 'appeal')),
    practice_area     TEXT NOT NULL DEFAULT 'employment',
    claim_categories  TEXT,                     -- free text tags: wage-hour, misclassification, discrimination, non-compete
    forum_type        TEXT NOT NULL DEFAULT 'federal_district'
                      CHECK (forum_type IN ('federal_district', 'federal_appeals', 'state_trial', 'state_appeals',
                                            'aaa', 'jams', 'other_arbitration', 'agency', 'pre_suit')),
    forum_name        TEXT,                     -- "U.S. District Court, N.D. Ill." / "Circuit Court of Cook County, Law Division"
    rules_profile     TEXT,                     -- rules file id, e.g. 'ndil', 'ca7', 'cook', 'aaa_employment', 'jams_employment'
    case_number       TEXT,
    judge             TEXT,
    magistrate_judge  TEXT,
    arbitrator        TEXT,
    status            TEXT NOT NULL DEFAULT 'intake'
                      CHECK (status IN ('intake', 'active', 'stayed', 'settled_pending', 'appeal', 'closed', 'declined')),
    phase             TEXT
                      CHECK (phase IN ('pre_suit', 'pleadings', 'discovery', 'dispositive_motions', 'class_cert',
                                       'mediation', 'trial', 'post_trial', 'appeal', 'settlement_admin')),
    ai_permitted      INTEGER NOT NULL DEFAULT 0 CHECK (ai_permitted IN (0, 1)),  -- client consent to AI use
    commenced_on      TEXT,                     -- complaint/demand filed
    served_on         TEXT,                     -- our client served (or we served)
    opened_on         TEXT NOT NULL DEFAULT (date('now')),
    closed_on         TEXT,
    folder_path       TEXT,                     -- absolute path to the matter folder
    summary           TEXT,                     -- one-paragraph case summary
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Roles a party plays in a matter (workbook: People)
CREATE TABLE matter_parties (
    id          INTEGER PRIMARY KEY,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    party_id    INTEGER NOT NULL REFERENCES parties(id) ON DELETE RESTRICT,
    role        TEXT NOT NULL
                CHECK (role IN ('client', 'client_contact', 'adverse_party', 'opposing_counsel', 'co_party',
                                'co_counsel', 'related_entity', 'witness', 'decision_maker', 'comparator',
                                'class_member', 'opt_in', 'expert', 'insurer', 'mediator', 'other')),
    title       TEXT,                           -- job title / position at relevant time
    represented_by_party_id INTEGER REFERENCES parties(id),
    key_person  INTEGER NOT NULL DEFAULT 0 CHECK (key_person IN (0, 1)),
    notes       TEXT,
    UNIQUE (matter_id, party_id, role)
);
CREATE INDEX idx_matter_parties_matter ON matter_parties(matter_id);
CREATE INDEX idx_matter_parties_party ON matter_parties(party_id);

-- ---------------------------------------------------------------------------
-- Claims and elements (workbook: Claims; wiki: 20-Claims)
-- ---------------------------------------------------------------------------
CREATE TABLE claims (
    id            INTEGER PRIMARY KEY,
    matter_id     INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    claim_no      TEXT,                         -- "Count I"
    title         TEXT NOT NULL,                -- "FLSA overtime"
    legal_basis   TEXT,                         -- "29 U.S.C. 207"
    asserted_by   TEXT CHECK (asserted_by IN ('plaintiff', 'defendant', 'counter', 'third_party')),
    procedure     TEXT CHECK (procedure IN ('individual', 'class', 'collective', 'representative')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('anticipated', 'pending', 'dismissed', 'withdrawn', 'resolved_sj',
                                    'tried', 'settled')),
    limitations   TEXT,                         -- limitations period and accrual notes
    damages_theory TEXT,
    assessment    TEXT,                         -- our view of strength
    notes         TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_claims_matter ON claims(matter_id);

CREATE TABLE claim_elements (
    id         INTEGER PRIMARY KEY,
    claim_id   INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    element    TEXT NOT NULL,
    proof      TEXT,                            -- evidence that proves or defeats it
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'strong', 'contested', 'weak')),
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE defenses (
    id         INTEGER PRIMARY KEY,
    matter_id  INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    claim_id   INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    title      TEXT NOT NULL,
    basis      TEXT,
    status     TEXT NOT NULL DEFAULT 'pleaded'
               CHECK (status IN ('considering', 'pleaded', 'waived', 'stricken', 'prevailed', 'rejected')),
    notes      TEXT
);

-- ---------------------------------------------------------------------------
-- Issues (wiki: 60-Issues; feeds the "Lay of the Law" issue frame)
-- ---------------------------------------------------------------------------
CREATE TABLE issues (
    id         INTEGER PRIMARY KEY,
    matter_id  INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    question   TEXT,                            -- the issue framed as a question
    our_position TEXT,
    priority   TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('high', 'medium', 'low')),
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'briefed', 'decided', 'moot')),
    notes      TEXT
);

-- ---------------------------------------------------------------------------
-- Documents (workbook: Document Log; wiki: 40-Documents) + page text for search
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    id           INTEGER PRIMARY KEY,
    matter_id    INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    doc_type     TEXT NOT NULL DEFAULT 'other'
                 CHECK (doc_type IN ('complaint', 'answer', 'pleading', 'motion', 'brief', 'order', 'judgment',
                                     'correspondence', 'email', 'discovery_request', 'discovery_response',
                                     'production', 'transcript', 'exhibit', 'declaration', 'contract',
                                     'policy', 'payroll_data', 'time_data', 'research', 'work_product',
                                     'demand', 'award', 'other')),
    doc_date     TEXT,
    author       TEXT,
    recipients   TEXT,
    bates_begin  TEXT,
    bates_end    TEXT,
    docket_no    TEXT,                          -- ECF number if filed
    source       TEXT CHECK (source IN ('client', 'opposing', 'court', 'third_party', 'internal', 'email', 'public')),
    received_on  TEXT,
    file_path    TEXT NOT NULL,                 -- relative to matter folder
    sha256       TEXT,
    version      INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER REFERENCES documents(id),
    page_count   INTEGER,
    text_status  TEXT NOT NULL DEFAULT 'pending'
                 CHECK (text_status IN ('pending', 'extracted', 'low_text', 'ocr_done', 'failed', 'not_applicable')),
    privileged   INTEGER NOT NULL DEFAULT 0 CHECK (privileged IN (0, 1)),
    key_document INTEGER NOT NULL DEFAULT 0 CHECK (key_document IN (0, 1)),
    summary      TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_documents_matter ON documents(matter_id);
CREATE UNIQUE INDEX idx_documents_hash ON documents(matter_id, sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE document_pages (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,               -- 1-based, matches the [Page N] markers
    text        TEXT NOT NULL,
    PRIMARY KEY (document_id, page_no)
);

CREATE VIRTUAL TABLE document_pages_fts USING fts5(
    text,
    content = 'document_pages',
    tokenize = 'porter unicode61'
);
CREATE TRIGGER document_pages_ai AFTER INSERT ON document_pages BEGIN
    INSERT INTO document_pages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER document_pages_ad AFTER DELETE ON document_pages BEGIN
    INSERT INTO document_pages_fts(document_pages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER document_pages_au AFTER UPDATE ON document_pages BEGIN
    INSERT INTO document_pages_fts(document_pages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO document_pages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- ---------------------------------------------------------------------------
-- Chronology (workbook: Key Events; wiki: 10-Chronology)
-- ---------------------------------------------------------------------------
CREATE TABLE events (
    id             INTEGER PRIMARY KEY,
    matter_id      INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    event_code     TEXT NOT NULL,               -- "E-001", stable ID used by depo prep outlines
    event_date     TEXT,                        -- may be partial: YYYY or YYYY-MM
    date_precision TEXT NOT NULL DEFAULT 'day' CHECK (date_precision IN ('day', 'month', 'year', 'approximate', 'unknown')),
    description    TEXT NOT NULL,
    significance   TEXT NOT NULL DEFAULT 'supporting' CHECK (significance IN ('key', 'supporting', 'background')),
    disputed       INTEGER NOT NULL DEFAULT 0 CHECK (disputed IN (0, 1)),
    source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    pin            TEXT,                        -- "p. 4" or "45:3-12"
    issue_tags     TEXT,
    notes          TEXT,
    UNIQUE (matter_id, event_code)
);
CREATE INDEX idx_events_matter_date ON events(matter_id, event_date);

CREATE TABLE event_people (
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    matter_party_id INTEGER NOT NULL REFERENCES matter_parties(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, matter_party_id)
);

-- ---------------------------------------------------------------------------
-- Deadlines (computed by the rules engine; nothing is confirmed automatically)
-- ---------------------------------------------------------------------------
CREATE TABLE deadlines (
    id            INTEGER PRIMARY KEY,
    matter_id     INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    due_on        TEXT NOT NULL,
    due_time      TEXT,                         -- e.g. 23:59 for ECF, if relevant
    rule_id       TEXT,                         -- rules file entry id, e.g. 'frcp.answer'
    rule_cite     TEXT,                         -- "Fed. R. Civ. P. 12(a)(1)(A)(i)"
    trigger_event TEXT,
    trigger_date  TEXT,
    service_method TEXT CHECK (service_method IN ('electronic', 'mail', 'hand', 'leave_with_clerk', 'other_consented', 'not_applicable')),
    computation   TEXT,                         -- human-readable math shown to the user
    hard          INTEGER NOT NULL DEFAULT 1 CHECK (hard IN (0, 1)),
    status        TEXT NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'confirmed', 'done', 'vacated', 'superseded')),
    source        TEXT NOT NULL DEFAULT 'rules_engine'
                  CHECK (source IN ('rules_engine', 'court_order', 'stipulation', 'manual', 'ecf_notice')),
    confirmed_at  TEXT,
    calendar_event_id TEXT,                     -- Graph or Google Calendar event id once pushed
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_deadlines_due ON deadlines(status, due_on);

-- ---------------------------------------------------------------------------
-- Tasks (workbook: Task List)
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY,
    matter_id    INTEGER REFERENCES matters(id) ON DELETE CASCADE,   -- NULL = practice-level task
    title        TEXT NOT NULL,
    due_on       TEXT,
    priority     TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
    status       TEXT NOT NULL DEFAULT 'not_started'
                 CHECK (status IN ('not_started', 'in_progress', 'waiting', 'done', 'dropped')),
    deadline_id  INTEGER REFERENCES deadlines(id) ON DELETE SET NULL,
    source       TEXT CHECK (source IN ('manual', 'email', 'deadline', 'ai_suggested')),
    notes        TEXT,
    completed_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_tasks_open ON tasks(status, due_on);

-- ---------------------------------------------------------------------------
-- Docket (ECF notices, manual entries, CourtListener)
-- ---------------------------------------------------------------------------
CREATE TABLE docket_entries (
    id          INTEGER PRIMARY KEY,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    entry_no    TEXT,
    filed_on    TEXT NOT NULL,
    text        TEXT NOT NULL,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'ecf_email', 'courtlistener', 'aaa', 'jams')),
    UNIQUE (matter_id, entry_no)
);

-- ---------------------------------------------------------------------------
-- Discovery tracker (wiki: 50-Discovery; discovery matrix)
-- ---------------------------------------------------------------------------
CREATE TABLE discovery_sets (
    id              INTEGER PRIMARY KEY,
    matter_id       INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL CHECK (direction IN ('served', 'received')),
    set_type        TEXT NOT NULL CHECK (set_type IN ('interrogatories', 'rfp', 'rfa', 'subpoena', 'depo_notice', 'initial_disclosures', 'expert_disclosure')),
    set_label       TEXT,                       -- "Defendant's First Set of Interrogatories"
    party_matter_party_id INTEGER REFERENCES matter_parties(id),
    served_on       TEXT,
    service_method  TEXT CHECK (service_method IN ('electronic', 'mail', 'hand', 'other_consented')),
    request_count   INTEGER,
    response_due_on TEXT,
    deadline_id     INTEGER REFERENCES deadlines(id) ON DELETE SET NULL,
    responded_on    TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('drafting', 'open', 'responded', 'deficient', 'meet_confer', 'motion', 'resolved')),
    request_document_id  INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    response_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    notes           TEXT
);

-- ---------------------------------------------------------------------------
-- Conflicts checks (logged before a matter opens)
-- ---------------------------------------------------------------------------
CREATE TABLE conflict_checks (
    id          INTEGER PRIMARY KEY,
    checked_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    names       TEXT NOT NULL,                  -- JSON array of names searched
    results     TEXT NOT NULL,                  -- JSON array of hits
    outcome     TEXT NOT NULL CHECK (outcome IN ('clear', 'potential', 'conflict', 'waived')),
    matter_id   INTEGER REFERENCES matters(id) ON DELETE SET NULL,
    notes       TEXT
);

-- ---------------------------------------------------------------------------
-- AI gateway audit log (every model call)
-- ---------------------------------------------------------------------------
CREATE TABLE ai_calls (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    matter_id      INTEGER REFERENCES matters(id) ON DELETE SET NULL,
    purpose        TEXT NOT NULL,               -- 'reply_draft', 'chronology', 'qa', ...
    template       TEXT,
    template_version TEXT,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    document_ids   TEXT,                        -- JSON array of documents whose text was sent
    input_chars    INTEGER,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'error')),
    blocked_reason TEXT,
    error          TEXT
);

-- ---------------------------------------------------------------------------
-- Change log (wiki: 99-Changelog)
-- ---------------------------------------------------------------------------
CREATE TABLE changelog (
    id         INTEGER PRIMARY KEY,
    matter_id  INTEGER REFERENCES matters(id) ON DELETE CASCADE,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    actor      TEXT NOT NULL CHECK (actor IN ('user', 'ai', 'system')),
    entity     TEXT NOT NULL,
    entity_id  INTEGER,
    summary    TEXT NOT NULL
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Dashboard view (workbook: Dashboard tab)
-- ---------------------------------------------------------------------------
CREATE VIEW v_matter_dashboard AS
SELECT
    m.id, m.short_name, m.caption, m.side, m.status, m.phase, m.forum_name, m.case_number, m.judge,
    (SELECT MIN(d.due_on) FROM deadlines d
      WHERE d.matter_id = m.id AND d.status IN ('proposed', 'confirmed')) AS next_deadline,
    (SELECT COUNT(*) FROM deadlines d
      WHERE d.matter_id = m.id AND d.status = 'proposed') AS unconfirmed_deadlines,
    (SELECT COUNT(*) FROM tasks t
      WHERE t.matter_id = m.id AND t.status NOT IN ('done', 'dropped')) AS open_tasks,
    (SELECT COUNT(*) FROM documents doc WHERE doc.matter_id = m.id) AS documents,
    (SELECT MAX(e.event_date) FROM events e WHERE e.matter_id = m.id) AS last_event
FROM matters m;
