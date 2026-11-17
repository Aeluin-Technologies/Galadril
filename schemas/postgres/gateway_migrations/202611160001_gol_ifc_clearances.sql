-- Explicit ceilings belong to a trusted authority, independently of IAM roles.
CREATE TABLE IF NOT EXISTS iam_ifc_clearances (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    classification SMALLINT NOT NULL CHECK (classification BETWEEN 0 AND 3),
    compartments TEXT[] NOT NULL CHECK (
        cardinality(compartments) <= 128
        AND array_position(compartments, NULL) IS NULL
        AND octet_length(array_to_string(compartments, ',')) <= 32768
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES iam_users (tenant_id, user_id) ON DELETE CASCADE
);

ALTER TABLE iam_ifc_clearances ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam_ifc_clearances FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON iam_ifc_clearances;
CREATE POLICY tenant_isolation ON iam_ifc_clearances
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
REVOKE ALL ON iam_ifc_clearances FROM PUBLIC;

-- Gateway consumes approvals; this migration deliberately creates no grants.
DO $clearance$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'galadril_app') THEN
        REVOKE ALL ON iam_ifc_clearances FROM galadril_app;
        GRANT SELECT ON iam_ifc_clearances TO galadril_app;
    END IF;
END
$clearance$;
