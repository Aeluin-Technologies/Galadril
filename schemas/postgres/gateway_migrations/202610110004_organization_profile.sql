-- Display metadata never establishes membership or links authentication identities.
ALTER TABLE sso_tenant_settings ADD COLUMN IF NOT EXISTS organization_name TEXT NOT NULL DEFAULT ''
    CHECK (length(organization_name) <= 128);
ALTER TABLE iam_users ADD COLUMN IF NOT EXISTS display_name TEXT
    CHECK (length(display_name) <= 128);
ALTER TABLE iam_users ADD COLUMN IF NOT EXISTS email TEXT
    CHECK (length(email) <= 254);
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'galadril_auth') THEN
        GRANT UPDATE (display_name,email) ON iam_users TO galadril_auth;
    END IF;
END $$;
