-- Makes token replay durable without persisting model reasoning or URLs.

ALTER TABLE conversation_messages
ADD COLUMN IF NOT EXISTS generation_sequence INTEGER NOT NULL DEFAULT 0
CHECK (generation_sequence BETWEEN 0 AND 4096);

CREATE TABLE IF NOT EXISTS conversation_generation_events (
    tenant_id TEXT NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    generation_id CHAR(32) NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 4096),
    kind TEXT NOT NULL CHECK (kind IN ('content', 'completed', 'failed')),
    content TEXT NOT NULL DEFAULT '' CHECK (octet_length(content) <= 65536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, conversation_id, generation_id, sequence),
    FOREIGN KEY (tenant_id, conversation_id, generation_id)
        REFERENCES conversation_messages (tenant_id, conversation_id, message_id)
);

ALTER TABLE conversation_generation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_generation_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON conversation_generation_events;
CREATE POLICY tenant_isolation ON conversation_generation_events FOR ALL
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
REVOKE ALL ON conversation_generation_events FROM PUBLIC;
GRANT SELECT, INSERT ON conversation_generation_events TO galadril_app;
DROP TRIGGER IF EXISTS conversation_generation_events_immutable ON conversation_generation_events;
CREATE TRIGGER conversation_generation_events_immutable
BEFORE UPDATE OR DELETE ON conversation_generation_events
FOR EACH ROW EXECUTE FUNCTION reject_control_plane_history_mutation();

CREATE INDEX IF NOT EXISTS conversations_active_list
ON conversations (tenant_id, updated_at DESC, conversation_id)
WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS conversation_messages_active_history
ON conversation_messages (tenant_id, conversation_id, created_at, message_id)
WHERE deleted_at IS NULL;
