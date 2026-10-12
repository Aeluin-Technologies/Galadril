-- Manual grants and provider-managed sources must survive independent updates.
ALTER TABLE iam_user_roles ADD COLUMN IF NOT EXISTS manual BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS sso_role_sync_configs (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    configuration JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id)
);

CREATE TABLE IF NOT EXISTS iam_federated_role_assignments (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, connection_id, role_name),
    FOREIGN KEY (tenant_id, user_id, role_name)
        REFERENCES iam_user_roles (tenant_id, user_id, role_name) ON DELETE CASCADE
);

DO $rls$
DECLARE tenant_table TEXT;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY['sso_role_sync_configs', 'iam_federated_role_assignments'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))', tenant_table);
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', tenant_table);
        IF EXISTS (SELECT FROM pg_roles WHERE rolname='galadril_app') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO galadril_app', tenant_table);
        END IF;
    END LOOP;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='galadril_auth') THEN
        GRANT SELECT ON sso_role_sync_configs, iam_roles TO galadril_auth;
        GRANT SELECT, INSERT, DELETE ON iam_federated_role_assignments, iam_user_roles TO galadril_auth;
    END IF;
END
$rls$;
