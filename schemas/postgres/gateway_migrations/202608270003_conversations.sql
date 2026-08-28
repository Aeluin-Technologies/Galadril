-- Creates versioned Scribe conversation, message, and attachment tables.

CREATE TABLE IF NOT EXISTS conversations (
    tenant_id TEXT NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    active_generation_id CHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, conversation_id),
    CHECK (owner_id <> ''),
    CHECK (char_length(title) BETWEEN 1 AND 256),
    CHECK (revision >= 0)
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    tenant_id TEXT NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    message_id CHAR(32) NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model_alias TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    revision BIGINT NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, conversation_id, message_id),
    FOREIGN KEY (tenant_id, conversation_id)
        REFERENCES conversations (tenant_id, conversation_id),
    CHECK (role IN ('user', 'assistant', 'system')),
    CHECK (status IN ('pending', 'completed', 'failed')),
    CHECK (revision > 0),
    CHECK (created_by <> '')
);

CREATE TABLE IF NOT EXISTS conversation_message_revisions (
    tenant_id TEXT NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    message_id CHAR(32) NOT NULL,
    revision BIGINT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
    deleted_at TIMESTAMPTZ,
    changed_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, conversation_id, message_id, revision),
    CHECK (revision > 0),
    CHECK (status IN ('pending', 'completed', 'failed')),
    CHECK (jsonb_typeof(attachments) = 'array'),
    CHECK (changed_by <> '')
);

CREATE TABLE IF NOT EXISTS conversation_message_attachments (
    tenant_id TEXT NOT NULL,
    conversation_id CHAR(32) NOT NULL,
    message_id CHAR(32) NOT NULL,
    object_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_name TEXT,
    content_type TEXT,
    size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, conversation_id, message_id, object_key),
    FOREIGN KEY (tenant_id, conversation_id, message_id)
        REFERENCES conversation_messages (
            tenant_id, conversation_id, message_id
        ),
    CHECK (object_key <> ''),
    CHECK (kind IN ('image', 'audio')),
    CHECK (size_bytes IS NULL OR size_bytes >= 0)
);

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'conversations_active_generation_fk'
    ) THEN
        ALTER TABLE conversations
        ADD CONSTRAINT conversations_active_generation_fk
        FOREIGN KEY (tenant_id, conversation_id, active_generation_id)
        REFERENCES conversation_messages (
            tenant_id, conversation_id, message_id
        ) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'message_revisions_current_message_fk'
    ) THEN
        ALTER TABLE conversation_message_revisions
        ADD CONSTRAINT message_revisions_current_message_fk
        FOREIGN KEY (tenant_id, conversation_id, message_id)
        REFERENCES conversation_messages (
            tenant_id, conversation_id, message_id
        ) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$constraints$;
