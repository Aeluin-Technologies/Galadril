-- A retry or concurrent enrollment must never replace a signer's private key.
CREATE TABLE IF NOT EXISTS pengolo_signing_vaults (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL CHECK (owner_id <> ''),
    envelope TEXT NOT NULL CHECK (octet_length(envelope) BETWEEN 1 AND 10000),
    PRIMARY KEY (tenant_id, owner_id)
);

ALTER TABLE pengolo_signing_vaults ENABLE ROW LEVEL SECURITY;
ALTER TABLE pengolo_signing_vaults FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS personal_signing_vault_scope ON pengolo_signing_vaults;
CREATE POLICY personal_signing_vault_scope ON pengolo_signing_vaults
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.signature_owner', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.signature_owner', true)
    );
