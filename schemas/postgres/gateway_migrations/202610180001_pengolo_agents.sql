-- Agent missions stay tenant-isolated and personal even in shared projects.
ALTER TABLE pengolo_records DROP CONSTRAINT pengolo_records_kind_check;
ALTER TABLE pengolo_records ADD CONSTRAINT pengolo_records_kind_check
    CHECK (kind IN ('chat','project','file','generation','agent'));
CREATE INDEX pengolo_agents_owner ON pengolo_records (tenant_id, owner_id, updated_at DESC, id DESC)
    WHERE kind = 'agent';
