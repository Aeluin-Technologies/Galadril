-- External identity ownership is explicit; email is only a discovery hint.
CREATE TABLE IF NOT EXISTS sso_connections (
    domain TEXT PRIMARY KEY CHECK (domain = lower(domain)),
    tenant_id TEXT NOT NULL CHECK (tenant_id ~ '^[a-zA-Z0-9_-]{1,64}$'),
    connection_id TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    jit_provision BOOLEAN NOT NULL DEFAULT FALSE,
    permission_import BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS iam_external_identities (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    external_subject TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, connection_id, external_subject),
    FOREIGN KEY (tenant_id, user_id) REFERENCES iam_users (tenant_id, user_id)
);
ALTER TABLE iam_external_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam_external_identities FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON iam_external_identities;
CREATE POLICY tenant_isolation ON iam_external_identities
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
REVOKE ALL ON sso_connections, iam_external_identities FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'galadril_auth') THEN
        GRANT SELECT ON sso_connections TO galadril_auth;
        GRANT SELECT, INSERT ON iam_external_identities TO galadril_auth;
        GRANT SELECT, INSERT ON iam_users TO galadril_auth;
    END IF;
END $$;
