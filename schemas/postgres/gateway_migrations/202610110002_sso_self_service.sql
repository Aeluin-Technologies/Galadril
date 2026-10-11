-- Polis is the sole encrypted secret store; routing metadata contains references.
ALTER TABLE sso_connections DROP COLUMN IF EXISTS client_secret;
ALTER TABLE sso_connections DROP COLUMN IF EXISTS permission_import;
ALTER TABLE sso_connections ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'oidc'
    CHECK (protocol IN ('oidc', 'saml'));

CREATE TABLE IF NOT EXISTS sso_tenant_settings (
    tenant_id TEXT PRIMARY KEY,
    permission_import BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE sso_tenant_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE sso_tenant_settings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sso_tenant_settings;
CREATE POLICY tenant_isolation ON sso_tenant_settings
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE TABLE IF NOT EXISTS sso_domain_challenges (
    tenant_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    proof TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 day',
    PRIMARY KEY (tenant_id, domain)
);
ALTER TABLE sso_domain_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE sso_domain_challenges FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sso_domain_challenges;
CREATE POLICY tenant_isolation ON sso_domain_challenges
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Global routing reservations prevent duplicate domains during first enrollment.
CREATE TABLE IF NOT EXISTS sso_domain_ownership (
    domain TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL
);
INSERT INTO sso_domain_ownership(domain, tenant_id)
    SELECT domain, tenant_id FROM sso_connections
    ON CONFLICT (domain) DO NOTHING;
ALTER TABLE sso_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE sso_connections FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sso_connections;
CREATE POLICY tenant_isolation ON sso_connections
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
-- Only the constrained login role can resolve a domain before knowing a tenant.
DROP POLICY IF EXISTS auth_discovery ON sso_connections;
CREATE POLICY auth_discovery ON sso_connections FOR SELECT
    USING (current_user = 'galadril_auth');
ALTER TABLE sso_domain_ownership ENABLE ROW LEVEL SECURITY;
ALTER TABLE sso_domain_ownership FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sso_domain_ownership;
CREATE POLICY tenant_isolation ON sso_domain_ownership
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
REVOKE ALL ON sso_domain_ownership FROM PUBLIC;

CREATE TABLE IF NOT EXISTS sso_organizations (
    request_id UUID PRIMARY KEY,
    organization_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    external_subject TEXT NOT NULL,
    enrollment_token UUID NOT NULL,
    enrollment_until TIMESTAMPTZ NOT NULL DEFAULT now() + interval '2 minutes',
    ready BOOLEAN NOT NULL DEFAULT FALSE
);
REVOKE ALL ON sso_tenant_settings, sso_domain_challenges, sso_organizations FROM PUBLIC;
