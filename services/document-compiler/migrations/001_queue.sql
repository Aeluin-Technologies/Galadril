PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS compilation_jobs (
    id TEXT PRIMARY KEY NOT NULL,
    tenant TEXT NOT NULL,
    actor TEXT NOT NULL,
    generation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    capability TEXT NOT NULL,
    template_id TEXT NOT NULL,
    template_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
    failure TEXT NOT NULL DEFAULT '',
    lease TEXT NOT NULL DEFAULT '',
    lease_until BIGINT NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS compilation_queue ON compilation_jobs(status, lease_until, created_at);
CREATE INDEX IF NOT EXISTS compilation_tenant ON compilation_jobs(tenant, status);
