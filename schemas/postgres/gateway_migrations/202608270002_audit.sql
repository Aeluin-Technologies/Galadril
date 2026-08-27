-- Creates durable, append-only Gateway audit history.

CREATE TABLE IF NOT EXISTS audit_events (
    tenant_id TEXT NOT NULL,
    audit_id CHAR(32) NOT NULL,
    operation_id CHAR(32) NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    failure_kind TEXT,
    request_id TEXT NOT NULL,
    trace_id CHAR(32),
    revision_id TEXT,
    publication_id TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, audit_id),
    CHECK (tenant_id <> ''),
    CHECK (actor_type <> ''),
    CHECK (actor_id <> ''),
    CHECK (action <> ''),
    CHECK (resource_type <> ''),
    CHECK (resource_id <> ''),
    CHECK (request_id <> ''),
    CHECK (outcome IN ('attempted', 'succeeded', 'failed', 'denied')),
    CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_occurred_at
ON audit_events (tenant_id, occurred_at DESC, audit_id DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_resource
ON audit_events (tenant_id, resource_type, resource_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_operation
ON audit_events (tenant_id, operation_id, occurred_at ASC);
