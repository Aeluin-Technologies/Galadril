-- Current Gateway-owned PostgreSQL schema for fresh environments.

CREATE TABLE iam_users (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE iam_roles (
    tenant_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, role_name)
);

CREATE TABLE iam_user_roles (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, role_name),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES iam_users (tenant_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, role_name)
        REFERENCES iam_roles (tenant_id, role_name) ON DELETE CASCADE
);

CREATE TABLE auth_policies (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    content TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, id)
);

DO $rls$
DECLARE
    tenant_table TEXT;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY[
        'iam_users', 'iam_roles', 'iam_user_roles', 'auth_policies'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tenant_table
        );
        EXECUTE format(
            'ALTER TABLE %I FORCE ROW LEVEL SECURITY', tenant_table
        );
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I FOR ALL '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
            tenant_table
        );
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', tenant_table);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO galadril_app',
            tenant_table
        );
    END LOOP;
END
$rls$;
