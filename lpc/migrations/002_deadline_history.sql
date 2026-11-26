-- 002: keep the history of a deadline whose date changes.
-- Moving a deadline marks the old row 'superseded' and adds a new row that points back to it.
ALTER TABLE deadlines ADD COLUMN supersedes_id INTEGER REFERENCES deadlines(id);
CREATE INDEX idx_deadlines_matter ON deadlines(matter_id, status, due_on);
