-- Existing installations need the same provenance index as newly created tables.
CREATE INDEX IF NOT EXISTS idx_eskg_document_evidence
ON eskg_events USING gin ((properties->'document_evidence') jsonb_path_ops);
