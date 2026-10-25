-- Personal instructions never inherit a project's shared access or retention.
ALTER TABLE pengolo_records DROP CONSTRAINT IF EXISTS pengolo_records_kind_check;
ALTER TABLE pengolo_records ADD CONSTRAINT pengolo_records_kind_check
    CHECK (kind IN ('chat','project','file','generation','agent','preference'));
ALTER TABLE pengolo_records ADD CONSTRAINT pengolo_preferences_private
    CHECK (kind <> 'preference' OR (project_id IS NULL AND expires_at IS NULL));
CREATE UNIQUE INDEX pengolo_preferences_owner ON pengolo_records (tenant_id, owner_id)
    WHERE kind = 'preference';
