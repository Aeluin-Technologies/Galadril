//! RLS-bound SSO metadata; credentials remain exclusively in encrypted Polis.

use anyhow::{Context, Result, ensure};
use sqlx::Row;
use uuid::Uuid;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::sso::{Organization, SsoStore};
use crate::domain::sso::{ConnectionInput, ConnectionView, SsoSettings};

pub struct PgSsoStore {
    database: Database,
}

impl PgSsoStore {
    /// Shares the existing RLS-aware pool rather than introducing credentials.
    pub fn new(database: Database) -> Self {
        Self { database }
    }
}

#[async_trait::async_trait]
impl SsoStore for PgSsoStore {
    async fn reserve(
        &self,
        request_id: &str,
        domain: &str,
        external_subject: &str,
    ) -> Result<Organization> {
        let request = Uuid::parse_str(request_id)?;
        let mut tx = self.database.tenant("sso_provisioning").await?;
        let row = sqlx::query(r#"
            INSERT INTO sso_organizations (request_id,organization_id,owner_id,domain,external_subject,enrollment_token)
            VALUES ($1::text::uuid,$2,$3,$4,$5,$6::text::uuid)
            ON CONFLICT (request_id)
            DO UPDATE
            SET enrollment_token=EXCLUDED.enrollment_token,enrollment_until=now()+interval '2 minutes'
            WHERE sso_organizations.ready OR sso_organizations.enrollment_until<now()
            RETURNING organization_id AS tenant_id,owner_id,domain,external_subject,ready,enrollment_token::text
            "#)
            .bind(request.to_string()).bind(Uuid::new_v4().to_string()).bind(Uuid::new_v4().to_string()).bind(domain).bind(external_subject).bind(Uuid::new_v4().to_string())
            .fetch_optional(&mut *tx).await.context("Organization reservation failed")?.context("Organization enrollment already in progress")?;
        ensure!(
            row.try_get::<String, _>("domain")? == domain &&
                row.try_get::<String, _>("external_subject")? ==
                    external_subject,
            "Provisioning request changed"
        );
        let organization = Organization {
            tenant_id: row.try_get("tenant_id")?,
            owner_id: row.try_get("owner_id")?,
            ready: row.try_get("ready")?,
            enrollment_token: row.try_get("enrollment_token")?,
        };
        tx.commit().await?;
        Ok(organization)
    }

    async fn settings(&self, tenant: &str) -> Result<SsoSettings> {
        let mut tx = self.database.tenant(tenant).await?;
        let organization_name: String = sqlx::query_scalar("SELECT COALESCE((SELECT organization_name FROM sso_tenant_settings WHERE tenant_id=$1),'')").bind(tenant).fetch_one(&mut *tx).await?;
        let enabled: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM sso_tenant_settings WHERE tenant_id=$1 AND permission_import)").bind(tenant).fetch_one(&mut *tx).await?;
        let passkey_mfa: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM sso_tenant_settings WHERE tenant_id=$1 AND passkey_mfa)").bind(tenant).fetch_one(&mut *tx).await?;
        let rows = sqlx::query(
            r#"
            SELECT connection_id,domain,protocol,enabled,jit_provision
            FROM sso_connections
            WHERE tenant_id=$1 ORDER BY domain LIMIT 64
            "#,
        )
        .bind(tenant)
        .fetch_all(&mut *tx)
        .await?;
        let connections = rows
            .into_iter()
            .map(|row| {
                Ok(ConnectionView {
                    connection_id: row.try_get("connection_id")?,
                    domain: row.try_get("domain")?,
                    protocol: row.try_get("protocol")?,
                    enabled: row.try_get("enabled")?,
                    jit_provision: row.try_get("jit_provision")?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        tx.commit().await?;
        Ok(SsoSettings {
            organization_name,
            permission_import: enabled,
            passkey_mfa,
            connections,
        })
    }

    async fn session_allowed(
        &self,
        tenant: &str,
        user: &str,
        assurance: Option<&str>,
        generation: Option<i64>,
    ) -> Result<bool> {
        let mut tx = self.database.tenant(tenant).await?;
        let allowed: bool = sqlx::query_scalar(r#"
            SELECT EXISTS(SELECT FROM iam_users u
              LEFT JOIN sso_tenant_settings t ON t.tenant_id=u.tenant_id
              WHERE u.tenant_id=$1 AND u.user_id=$2 AND u.is_active AND u.deleted_at IS NULL
                AND (NOT COALESCE(t.passkey_mfa,false) OR
                  ($3::text='urn:galadril:webauthn:uv' AND EXISTS(
                    SELECT FROM iam_passkey_state s JOIN iam_passkeys k USING(tenant_id,user_id)
                    WHERE s.tenant_id=u.tenant_id AND s.user_id=u.user_id AND s.generation=$4::bigint))))
            "#).bind(tenant).bind(user).bind(assurance).bind(generation).fetch_one(&mut *tx).await?;
        tx.commit().await?;
        Ok(allowed)
    }

    async fn organization_name(&self, tenant: &str, name: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        sqlx::query("INSERT INTO sso_tenant_settings(tenant_id,organization_name) VALUES($1,$2) ON CONFLICT(tenant_id) DO UPDATE SET organization_name=EXCLUDED.organization_name")
            .bind(tenant).bind(name).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    async fn passkey_setting(
        &self,
        tenant: &str,
        enabled: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        sqlx::query("INSERT INTO sso_tenant_settings(tenant_id,passkey_mfa) VALUES($1,$2) ON CONFLICT(tenant_id) DO UPDATE SET passkey_mfa=EXCLUDED.passkey_mfa")
            .bind(tenant).bind(enabled).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    async fn reset_passkeys(&self, tenant: &str, user: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let generation: Option<i64> = sqlx::query_scalar("INSERT INTO iam_passkey_state(tenant_id,user_id,generation) SELECT tenant_id,user_id,1 FROM iam_users WHERE tenant_id=$1 AND user_id=$2 AND is_active AND deleted_at IS NULL ON CONFLICT(tenant_id,user_id) DO UPDATE SET generation=iam_passkey_state.generation+1 RETURNING generation")
            .bind(tenant).bind(user).fetch_optional(&mut *tx).await?;
        ensure!(generation.is_some(), "Active tenant account required");
        sqlx::query(
            "DELETE FROM iam_passkeys WHERE tenant_id=$1 AND user_id=$2",
        )
        .bind(tenant)
        .bind(user)
        .execute(&mut *tx)
        .await?;
        sqlx::query("DELETE FROM iam_passkey_challenges WHERE tenant_id=$1 AND user_id=$2").bind(tenant).bind(user).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    async fn challenge(
        &self,
        tenant: &str,
        domain: &str,
        proof: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        sqlx::query(
            r#"
            INSERT INTO sso_domain_challenges (tenant_id,domain,proof)
            VALUES ($1,$2,$3)
            ON CONFLICT (tenant_id,domain)
            DO UPDATE
            SET proof=EXCLUDED.proof,expires_at=now()+interval '1 day'
            "#,
        )
        .bind(tenant)
        .bind(domain)
        .bind(proof)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn proof(&self, tenant: &str, domain: &str) -> Result<String> {
        let mut tx = self.database.tenant(tenant).await?;
        let proof = sqlx::query_scalar("SELECT proof FROM sso_domain_challenges WHERE tenant_id=$1 AND domain=$2 AND expires_at>now()").bind(tenant).bind(domain).fetch_one(&mut *tx).await.context("Request a domain ownership challenge")?;
        tx.commit().await?;
        Ok(proof)
    }

    async fn connection(
        &self,
        tenant: &str,
        input: &ConnectionInput,
        connection_id: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let updated = sqlx::query(r#"
            INSERT INTO sso_connections(domain,tenant_id,connection_id,enabled,jit_provision,protocol)
            VALUES($1,$2,$3,true,$4,$5)
            ON CONFLICT(domain)
            DO UPDATE
            SET enabled=true,jit_provision=EXCLUDED.jit_provision,protocol=EXCLUDED.protocol
            WHERE sso_connections.tenant_id=EXCLUDED.tenant_id
            AND sso_connections.connection_id=EXCLUDED.connection_id
            "#)
            .bind(&input.domain).bind(tenant).bind(connection_id).bind(input.jit_provision).bind(if input.oidc_issuer.is_some() {"oidc"} else {"saml"}).execute(&mut *tx).await?;
        ensure!(
            updated.rows_affected() == 1,
            "Domain or identity binding already belongs to another connection"
        );
        tx.commit().await?;
        Ok(())
    }

    async fn claim_domain(
        &self,
        tenant: &str,
        domain: &str,
    ) -> Result<Option<String>> {
        let mut tx = self.database.tenant(tenant).await?;
        let claimed = sqlx::query(
            r#"
            INSERT INTO sso_domain_ownership(domain,tenant_id)
            VALUES($1,$2)
            ON CONFLICT(domain)
            DO UPDATE
            SET tenant_id=EXCLUDED.tenant_id
            WHERE sso_domain_ownership.tenant_id=EXCLUDED.tenant_id
            "#,
        )
        .bind(domain)
        .bind(tenant)
        .execute(&mut *tx)
        .await?;
        ensure!(
            claimed.rows_affected() == 1,
            "Domain already belongs to another organization"
        );
        let reference = sqlx::query_scalar("SELECT connection_id FROM sso_connections WHERE domain=$1 AND tenant_id=$2")
            .bind(domain).bind(tenant).fetch_optional(&mut *tx).await?;
        tx.commit().await?;
        Ok(reference)
    }

    async fn import_setting(&self, tenant: &str, enabled: bool) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        sqlx::query(
            r#"
            INSERT INTO sso_tenant_settings(tenant_id,permission_import)
            VALUES($1,$2)
            ON CONFLICT(tenant_id)
            DO UPDATE
            SET permission_import=EXCLUDED.permission_import
            "#,
        )
        .bind(tenant)
        .bind(enabled)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn disable(&self, tenant: &str, domain: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let result = sqlx::query("UPDATE sso_connections SET enabled=false WHERE tenant_id=$1 AND domain=$2").bind(tenant).bind(domain).execute(&mut *tx).await?;
        ensure!(result.rows_affected() == 1, "Unknown SSO domain");
        tx.commit().await?;
        Ok(())
    }

    async fn owner(
        &self,
        organization: &Organization,
        connection: &str,
        external_subject: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(&organization.tenant_id).await?;
        sqlx::query("INSERT INTO iam_users(tenant_id,user_id) VALUES($1,$2) ON CONFLICT(tenant_id,user_id) DO NOTHING").bind(&organization.tenant_id).bind(&organization.owner_id).execute(&mut *tx).await?;
        let linked = sqlx::query(r#"
            INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id)
            SELECT $1,$2,$3,user_id
            FROM iam_users
            WHERE tenant_id=$1
            AND user_id=$4
            AND is_active
            AND deleted_at IS NULL
            ON CONFLICT(tenant_id,connection_id,external_subject)
            DO UPDATE
            SET user_id=EXCLUDED.user_id
            WHERE iam_external_identities.user_id=EXCLUDED.user_id
            "#).bind(&organization.tenant_id).bind(connection).bind(external_subject).bind(&organization.owner_id).execute(&mut *tx).await?;
        ensure!(
            linked.rows_affected() == 1,
            "Provisioned owner unavailable or identity already linked"
        );
        tx.commit().await?;
        Ok(())
    }

    async fn link_identity(
        &self,
        tenant: &str,
        domain: &str,
        subject: &str,
        user: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let linked = sqlx::query(r#"
            INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id)
            SELECT $1,c.connection_id,$3,u.user_id
            FROM sso_connections c
            JOIN iam_users u ON u.tenant_id=c.tenant_id
            WHERE c.tenant_id=$1
            AND c.domain=$2
            AND u.user_id=$4
            AND u.is_active
            AND u.deleted_at IS NULL
            ON CONFLICT(tenant_id,connection_id,external_subject)
            DO UPDATE
            SET user_id=EXCLUDED.user_id
            WHERE iam_external_identities.user_id=EXCLUDED.user_id
            "#)
            .bind(tenant).bind(domain).bind(subject).bind(user).execute(&mut *tx).await?;
        ensure!(
            linked.rows_affected() == 1,
            "Account unavailable or identity already linked to another account"
        );
        tx.commit().await?;
        Ok(())
    }

    async fn complete(&self, organization: &Organization) -> Result<()> {
        let mut tx = self.database.tenant(&organization.tenant_id).await?;
        let changed = sqlx::query(
            r#"
            UPDATE sso_organizations
            SET ready=true
            WHERE organization_id=$1
            AND enrollment_token=$2::text::uuid
            AND enrollment_until>now()
            "#,
        )
        .bind(&organization.tenant_id)
        .bind(&organization.enrollment_token)
        .execute(&mut *tx)
        .await?;
        ensure!(changed.rows_affected() == 1, "Enrollment lease expired");
        tx.commit().await?;
        Ok(())
    }

    async fn release(&self, organization: &Organization) -> Result<()> {
        let mut tx = self.database.tenant(&organization.tenant_id).await?;
        sqlx::query(
            r#"
            UPDATE sso_organizations
            SET enrollment_until=now()
            WHERE organization_id=$1
            AND enrollment_token=$2::text::uuid
            AND NOT ready
            "#,
        )
        .bind(&organization.tenant_id)
        .bind(&organization.enrollment_token)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;

    use super::*;

    #[tokio::test]
    async fn organization_enrollment_is_serialized_and_routing_is_tenant_isolated()
    -> Result<()> {
        let roles = "CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'app'; CREATE ROLE galadril_auth LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'auth'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;";
        let container = Postgres::default()
            .with_init_sql(roles.as_bytes().to_vec())
            .with_tag("18.6-alpine")
            .start()
            .await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        database.verify_security().await?;
        let store = PgSsoStore::new(database.clone());
        let request = Uuid::new_v4().to_string();
        let first = store
            .reserve(&request, "acme.example", "upstream-owner")
            .await?;
        assert!(
            store
                .reserve(&request, "acme.example", "upstream-owner")
                .await
                .is_err(),
            "Concurrent provisioning must not create multiple upstream clients"
        );
        store.release(&first).await?;
        let retry = store
            .reserve(&request, "acme.example", "upstream-owner")
            .await?;
        assert_eq!(retry.tenant_id, first.tenant_id);
        assert_eq!(retry.owner_id, first.owner_id);
        assert!(
            store.complete(&first).await.is_err(),
            "An expired attempt cannot publish a later attempt's organization"
        );
        let first = retry;
        let input: ConnectionInput = serde_json::from_str(
            r#"{"domain":"acme.example","oidc_issuer":"https://login.acme.example"}"#,
        )?;
        assert!(
            store
                .claim_domain(&first.tenant_id, "acme.example")
                .await?
                .is_none()
        );
        assert!(store.claim_domain("other", "acme.example").await.is_err());
        store.owner(&first, "connection", "upstream-owner").await?;
        store
            .connection(&first.tenant_id, &input, "connection")
            .await?;
        assert_eq!(
            store
                .claim_domain(&first.tenant_id, "acme.example")
                .await?
                .as_deref(),
            Some("connection")
        );
        assert!(store.settings("other").await?.connections.is_empty());
        assert!(!store.settings(&first.tenant_id).await?.permission_import);
        store.import_setting(&first.tenant_id, true).await?;
        assert!(store.settings(&first.tenant_id).await?.permission_import);
        assert!(!store.settings("other").await?.permission_import);
        store
            .link_identity(
                &first.tenant_id,
                "acme.example",
                "second-subject",
                &first.owner_id,
            )
            .await?;
        store
            .link_identity(
                &first.tenant_id,
                "acme.example",
                "second-subject",
                &first.owner_id,
            )
            .await?;
        assert!(
            store
                .link_identity(
                    "other",
                    "acme.example",
                    "second-subject",
                    &first.owner_id
                )
                .await
                .is_err()
        );
        assert!(!store.settings(&first.tenant_id).await?.passkey_mfa);
        store.organization_name(&first.tenant_id, "Acme").await?;
        assert_eq!(
            store.settings(&first.tenant_id).await?.organization_name,
            "Acme"
        );
        assert!(
            store
                .session_allowed(&first.tenant_id, &first.owner_id, None, None)
                .await?
        );
        store.passkey_setting(&first.tenant_id, true).await?;
        assert!(store.settings(&first.tenant_id).await?.permission_import);
        assert!(
            !store
                .session_allowed(&first.tenant_id, &first.owner_id, None, None)
                .await?
        );
        let mut tx = database.tenant(&first.tenant_id).await?;
        sqlx::query(
            "INSERT INTO iam_passkey_state(tenant_id,user_id) VALUES($1,$2)",
        )
        .bind(&first.tenant_id)
        .bind(&first.owner_id)
        .execute(&mut *tx)
        .await?;
        sqlx::query("INSERT INTO iam_passkeys(tenant_id,user_id,credential_id,public_key,counter,device_type,backed_up) VALUES($1,$2,'test-credential',$3,0,'singleDevice',false)").bind(&first.tenant_id).bind(&first.owner_id).bind(b"test-public-key".as_slice()).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO iam_passkey_challenges(flow_id,tenant_id,user_id,generation,challenge,operation) VALUES('test-flow',$1,$2,0,'test-challenge','authentication')").bind(&first.tenant_id).bind(&first.owner_id).execute(&mut *tx).await?;
        tx.commit().await?;
        let assurance = Some("urn:galadril:webauthn:uv");
        assert!(
            store
                .session_allowed(
                    &first.tenant_id,
                    &first.owner_id,
                    assurance,
                    Some(0)
                )
                .await?
        );
        assert!(
            !store
                .session_allowed("other", &first.owner_id, assurance, Some(0))
                .await?
        );
        assert!(
            store
                .reset_passkeys("other", &first.owner_id)
                .await
                .is_err()
        );
        assert!(
            store
                .session_allowed(
                    &first.tenant_id,
                    &first.owner_id,
                    assurance,
                    Some(0)
                )
                .await?
        );
        store
            .reset_passkeys(&first.tenant_id, &first.owner_id)
            .await?;
        assert!(
            !store
                .session_allowed(
                    &first.tenant_id,
                    &first.owner_id,
                    assurance,
                    Some(0)
                )
                .await?
        );
        let mut tx = database.tenant(&first.tenant_id).await?;
        let generation: i64 = sqlx::query_scalar("SELECT generation FROM iam_passkey_state WHERE tenant_id=$1 AND user_id=$2").bind(&first.tenant_id).bind(&first.owner_id).fetch_one(&mut *tx).await?;
        assert_eq!(generation, 1);
        let proofs: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM iam_passkey_challenges WHERE tenant_id=$1",
        )
        .bind(&first.tenant_id)
        .fetch_one(&mut *tx)
        .await?;
        assert_eq!(proofs, 0);
        sqlx::query("INSERT INTO iam_passkeys(tenant_id,user_id,credential_id,public_key,counter,device_type,backed_up) VALUES($1,$2,'replacement-credential',$3,0,'singleDevice',false)").bind(&first.tenant_id).bind(&first.owner_id).bind(b"test-public-key".as_slice()).execute(&mut *tx).await?;
        tx.commit().await?;
        assert!(
            !store
                .session_allowed(
                    &first.tenant_id,
                    &first.owner_id,
                    assurance,
                    Some(0)
                )
                .await?
        );
        assert!(
            store
                .session_allowed(
                    &first.tenant_id,
                    &first.owner_id,
                    assurance,
                    Some(1)
                )
                .await?
        );
        store.complete(&first).await?;
        let replay = store
            .reserve(&request, "acme.example", "upstream-owner")
            .await?;
        assert_eq!(replay.tenant_id, first.tenant_id);
        assert_eq!(replay.owner_id, first.owner_id);
        assert!(replay.ready);
        assert!(
            store
                .reserve(&request, "other.example", "upstream-owner")
                .await
                .is_err()
        );
        Ok(())
    }
}
