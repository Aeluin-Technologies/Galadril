-- Creates immutable pipeline definitions and revision history.

CREATE TABLE IF NOT EXISTS pipeline_definitions (
    tenant_id TEXT NOT NULL,
    pipeline_id TEXT NOT NULL,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    head_revision_id CHAR(32) NOT NULL,
    published_revision_id CHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, pipeline_id),
    CHECK (pipeline_id <> ''),
    CHECK (char_length(name) BETWEEN 1 AND 256),
    CHECK (owner_id <> '')
);

CREATE TABLE IF NOT EXISTS pipeline_revisions (
    tenant_id TEXT NOT NULL,
    pipeline_id TEXT NOT NULL,
    revision_id CHAR(32) NOT NULL,
    parent_revision_id CHAR(32),
    definition JSONB NOT NULL,
    author_id TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, pipeline_id, revision_id),
    FOREIGN KEY (tenant_id, pipeline_id)
        REFERENCES pipeline_definitions (tenant_id, pipeline_id),
    CHECK (jsonb_typeof(definition) = 'object'),
    CHECK (author_id <> ''),
    CHECK (message <> '')
);

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'pipeline_definitions_head_revision_fk'
    ) THEN
        ALTER TABLE pipeline_definitions
        ADD CONSTRAINT pipeline_definitions_head_revision_fk
        FOREIGN KEY (tenant_id, pipeline_id, head_revision_id)
        REFERENCES pipeline_revisions (
            tenant_id, pipeline_id, revision_id
        ) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'pipeline_definitions_published_revision_fk'
    ) THEN
        ALTER TABLE pipeline_definitions
        ADD CONSTRAINT pipeline_definitions_published_revision_fk
        FOREIGN KEY (tenant_id, pipeline_id, published_revision_id)
        REFERENCES pipeline_revisions (
            tenant_id, pipeline_id, revision_id
        ) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'pipeline_revisions_parent_fk'
    ) THEN
        ALTER TABLE pipeline_revisions
        ADD CONSTRAINT pipeline_revisions_parent_fk
        FOREIGN KEY (tenant_id, pipeline_id, parent_revision_id)
        REFERENCES pipeline_revisions (
            tenant_id, pipeline_id, revision_id
        ) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$constraints$;
