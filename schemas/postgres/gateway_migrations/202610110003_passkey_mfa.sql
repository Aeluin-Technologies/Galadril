-- Credentials belong to canonical IAM accounts; resets invalidate in-flight proofs.
ALTER TABLE sso_tenant_settings ADD COLUMN IF NOT EXISTS passkey_mfa BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS iam_passkey_state (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    user_handle BYTEA NOT NULL DEFAULT uuid_send(gen_random_uuid()),
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES iam_users ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS iam_passkeys (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    credential_id TEXT NOT NULL UNIQUE,
    public_key BYTEA NOT NULL,
    counter BIGINT NOT NULL CHECK (counter BETWEEN 0 AND 4294967295),
    transports TEXT[] NOT NULL DEFAULT '{}',
    device_type TEXT NOT NULL CHECK (device_type IN ('singleDevice', 'multiDevice')),
    backed_up BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES iam_passkey_state ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS iam_passkey_challenges (
    flow_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    generation BIGINT NOT NULL,
    challenge TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('registration', 'authentication')),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '5 minutes',
    FOREIGN KEY (tenant_id, user_id) REFERENCES iam_passkey_state ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS iam_passkey_challenges_expiry ON iam_passkey_challenges (expires_at);

ALTER TABLE iam_passkey_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam_passkey_state FORCE ROW LEVEL SECURITY;
ALTER TABLE iam_passkeys ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam_passkeys FORCE ROW LEVEL SECURITY;
ALTER TABLE iam_passkey_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam_passkey_challenges FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON iam_passkey_state;
CREATE POLICY tenant_isolation ON iam_passkey_state
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
DROP POLICY IF EXISTS tenant_isolation ON iam_passkeys;
CREATE POLICY tenant_isolation ON iam_passkeys
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
DROP POLICY IF EXISTS tenant_isolation ON iam_passkey_challenges;
CREATE POLICY tenant_isolation ON iam_passkey_challenges
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
REVOKE ALL ON iam_passkey_state, iam_passkeys, iam_passkey_challenges FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'galadril_auth') THEN
        GRANT SELECT ON sso_tenant_settings TO galadril_auth;
        GRANT SELECT, INSERT, UPDATE ON iam_passkey_state, iam_passkeys TO galadril_auth;
        GRANT SELECT, INSERT, UPDATE, DELETE ON iam_passkey_challenges TO galadril_auth;
    END IF;
END $$;
