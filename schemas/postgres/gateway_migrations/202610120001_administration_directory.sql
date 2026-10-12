-- Tenant hierarchy edges are serialized by Gateway before cycle detection.
CREATE TABLE IF NOT EXISTS iam_role_parents (
    tenant_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    parent_role_name TEXT NOT NULL,
    PRIMARY KEY (tenant_id, role_name),
    FOREIGN KEY (tenant_id, role_name) REFERENCES iam_roles (tenant_id, role_name),
    FOREIGN KEY (tenant_id, parent_role_name) REFERENCES iam_roles (tenant_id, role_name),
    CHECK (role_name <> parent_role_name)
);
CREATE INDEX IF NOT EXISTS iam_role_parents_parent_idx
    ON iam_role_parents (tenant_id, parent_role_name);
ALTER TABLE iam_role_parents ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam_role_parents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON iam_role_parents FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
REVOKE ALL ON iam_role_parents FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON iam_role_parents TO galadril_app;

-- Production images install pg_trgm; vanilla PostgreSQL remains usable in tests.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_opclass WHERE opcname = 'gin_trgm_ops') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS iam_users_search_idx ON iam_users USING gin ((user_id || '' '' || COALESCE(display_name, '''') || '' '' || COALESCE(email, '''')) gin_trgm_ops) WHERE deleted_at IS NULL';
        EXECUTE 'CREATE INDEX IF NOT EXISTS iam_roles_search_idx ON iam_roles USING gin (role_name gin_trgm_ops) WHERE deleted_at IS NULL';
    END IF;
END $$;
