-- Install the replacement guards before removing the copied legacy checks.
-- SQLx applies this migration transactionally; tenant RLS, ephemeral scope,
-- identifier constraints and JSON size limits remain enforced throughout.
ALTER TABLE pengolo_records ADD CONSTRAINT pengolo_records_agent_state_kind
    CHECK (kind IN ('chat','project','file','generation','agent','preference','agent_state'));
ALTER TABLE pengolo_records DROP CONSTRAINT pengolo_records_kind_check;
ALTER TABLE pengolo_records RENAME CONSTRAINT pengolo_records_agent_state_kind TO pengolo_records_kind_check;

ALTER TABLE pengolo_ephemeral_records ADD CONSTRAINT pengolo_ephemeral_agent_state_kind
    CHECK (kind IN ('chat','file','generation','agent','agent_state'));
ALTER TABLE pengolo_ephemeral_records DROP CONSTRAINT IF EXISTS pengolo_records_kind_check;
ALTER TABLE pengolo_ephemeral_records DROP CONSTRAINT IF EXISTS pengolo_ephemeral_records_kind_check;
ALTER TABLE pengolo_ephemeral_records RENAME CONSTRAINT pengolo_ephemeral_agent_state_kind TO pengolo_ephemeral_records_kind_check;

CREATE INDEX IF NOT EXISTS pengolo_agent_state_generation ON pengolo_records (tenant_id, (data->>'generation'))
    WHERE kind = 'agent_state';
