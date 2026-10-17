-- Typed activity shares the existing ordered, tenant-scoped replay stream.
ALTER TABLE pengolo_output DROP CONSTRAINT pengolo_output_kind_check;
ALTER TABLE pengolo_output ADD CONSTRAINT pengolo_output_kind_check
    CHECK (kind IN ('content','tool','activity','completed','failed','cancelled'));
ALTER TABLE pengolo_ephemeral_output DROP CONSTRAINT IF EXISTS pengolo_ephemeral_output_kind_check;
ALTER TABLE pengolo_ephemeral_output DROP CONSTRAINT IF EXISTS pengolo_output_kind_check;
ALTER TABLE pengolo_ephemeral_output ADD CONSTRAINT pengolo_ephemeral_output_kind_check
    CHECK (kind IN ('content','tool','activity','completed','failed','cancelled'));
