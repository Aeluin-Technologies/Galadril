-- Work revisions survive retries; lease tokens still fence obsolete workers.
ALTER TABLE retrieval_jobs ADD COLUMN work_revision TEXT
    CHECK (work_revision ~ '^[a-f0-9]{32}$');
ALTER TABLE retrieval_units ADD COLUMN enriched BOOLEAN NOT NULL DEFAULT FALSE;
