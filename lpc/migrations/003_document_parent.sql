-- 003: an email's attachments are stored as their own documents, linked back to the email.
ALTER TABLE documents ADD COLUMN parent_id INTEGER REFERENCES documents(id) ON DELETE CASCADE;
CREATE INDEX idx_documents_parent ON documents(parent_id);
