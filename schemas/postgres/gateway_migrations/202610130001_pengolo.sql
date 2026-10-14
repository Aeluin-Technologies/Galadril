-- Temporary content never enters durable conversation tables or their WAL.
CREATE TABLE IF NOT EXISTS pengolo_records (
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('chat','project','file','generation')),
    id TEXT NOT NULL CHECK (id ~ '^[a-f0-9]{32}$'),
    owner_id TEXT NOT NULL CHECK (owner_id <> ''),
    project_id TEXT,
    expires_at BIGINT,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at BIGINT NOT NULL,
    data JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object' AND octet_length(data::text) <= 2097152),
    PRIMARY KEY (tenant_id, kind, id)
);
CREATE INDEX IF NOT EXISTS pengolo_records_activity ON pengolo_records (tenant_id, kind, updated_at DESC, id DESC);
CREATE UNLOGGED TABLE IF NOT EXISTS pengolo_ephemeral_records (LIKE pengolo_records INCLUDING ALL);
ALTER TABLE pengolo_ephemeral_records ADD CHECK (kind <> 'project' AND project_id IS NULL AND expires_at IS NOT NULL);

CREATE TABLE IF NOT EXISTS pengolo_output (
    tenant_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    kind TEXT NOT NULL CHECK (kind IN ('content','tool','completed','failed','cancelled')),
    content TEXT NOT NULL CHECK (octet_length(content) <= 65536),
    PRIMARY KEY (tenant_id, generation_id, sequence)
);
CREATE UNLOGGED TABLE IF NOT EXISTS pengolo_ephemeral_output (LIKE pengolo_output INCLUDING ALL);

-- Only an exact, unguessable capability can reveal its delegation scope.
CREATE UNLOGGED TABLE IF NOT EXISTS pengolo_capabilities (
    tenant_id TEXT NOT NULL,
    token TEXT PRIMARY KEY CHECK (token ~ '^[a-f0-9]{64}$'),
    generation_id TEXT NOT NULL,
    temporary BOOLEAN NOT NULL,
    expires_at BIGINT NOT NULL
);

-- Maintenance sees identifiers and deadlines, never prompts or file contents.
CREATE TABLE IF NOT EXISTS pengolo_retention (
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    temporary BOOLEAN NOT NULL,
    object_key TEXT,
    staging_key TEXT,
    expires_at BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, kind, id, temporary)
);

DO $rls$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['pengolo_records','pengolo_ephemeral_records','pengolo_output','pengolo_ephemeral_output','pengolo_capabilities','pengolo_retention'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))', table_name);
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO galadril_app', table_name);
    END LOOP;
END
$rls$;
DROP POLICY IF EXISTS capability_lookup ON pengolo_capabilities;
CREATE POLICY capability_lookup ON pengolo_capabilities FOR SELECT
USING (token = NULLIF(current_setting('app.pengolo_capability', true), '') AND expires_at > (extract(epoch FROM clock_timestamp()) * 1000)::BIGINT);
DROP POLICY IF EXISTS retention_maintenance ON pengolo_retention;
CREATE POLICY retention_maintenance ON pengolo_retention FOR SELECT
USING (current_setting('app.pengolo_maintenance', true) = 'on' AND expires_at <= (extract(epoch FROM clock_timestamp()) * 1000)::BIGINT);

CREATE INDEX IF NOT EXISTS pengolo_scope_activity ON pengolo_records(tenant_id,kind,project_id,owner_id,updated_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS pengolo_generation_chat ON pengolo_records(tenant_id,(data->>'chatId')) WHERE kind='generation';
