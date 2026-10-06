-- Preserves authorization dependencies of generated and inherited answers.

ALTER TABLE conversation_messages
ADD COLUMN IF NOT EXISTS evidence_sources JSONB NOT NULL DEFAULT '[]'::jsonb
CHECK (jsonb_typeof(evidence_sources) = 'array' AND jsonb_array_length(evidence_sources) <= 512),
ADD COLUMN IF NOT EXISTS source_generation_id CHAR(32);

ALTER TABLE conversation_messages
DROP CONSTRAINT IF EXISTS message_source_generation_fk;
ALTER TABLE conversation_messages
ADD CONSTRAINT message_source_generation_fk
FOREIGN KEY (tenant_id, conversation_id, source_generation_id)
REFERENCES conversation_messages (tenant_id, conversation_id, message_id);
