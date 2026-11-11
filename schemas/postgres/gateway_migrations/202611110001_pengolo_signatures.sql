-- Personal marks cannot be read through project or tenant administration.
CREATE TABLE pengolo_signatures (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL CHECK (owner_id <> ''),
    id TEXT NOT NULL CHECK (id ~ '^[a-f0-9]{32}$'),
    label TEXT NOT NULL CHECK (octet_length(label) BETWEEN 1 AND 120),
    created_at BIGINT NOT NULL,
    sealed_trace BYTEA NOT NULL CHECK (octet_length(sealed_trace) BETWEEN 29 AND 16412),
    PRIMARY KEY (tenant_id, owner_id, id)
);

ALTER TABLE pengolo_signatures ENABLE ROW LEVEL SECURITY;
ALTER TABLE pengolo_signatures FORCE ROW LEVEL SECURITY;
CREATE POLICY personal_signature_scope ON pengolo_signatures
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.signature_owner', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.signature_owner', true)
    );
