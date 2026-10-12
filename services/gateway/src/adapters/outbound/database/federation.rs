//! RLS-bound federation configuration sharing Gateway's existing database
//! pool.

use anyhow::Result;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::federation::{
    FederationConfig, FederationStore,
};
use crate::authentication::federation;

pub struct PgFederationStore {
    database: Database,
}

impl PgFederationStore {
    /// Reuses the canonical application pool and its tenant transaction
    /// boundary.
    pub fn new(database: Database) -> Self {
        Self { database }
    }
}

#[async_trait::async_trait]
impl FederationStore for PgFederationStore {
    async fn configurations(
        &self,
        tenant: &str,
    ) -> Result<Vec<FederationConfig>> {
        let mut transaction = self.database.tenant(tenant).await?;
        let configurations =
            federation::configurations(&mut transaction, tenant).await?;
        transaction.commit().await?;
        Ok(configurations)
    }

    async fn configure(
        &self,
        tenant: &str,
        config: &FederationConfig,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant).await?;
        federation::configure(&mut transaction, tenant, config).await?;
        transaction.commit().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use anyhow::ensure;
    use serde_json::json;
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt as _;
    use testcontainers_modules::testcontainers::runners::AsyncRunner as _;

    use super::*;
    use crate::adapters::outbound::database::iam::PgIamStore;
    use crate::application::ports::iam_store::IamStore as _;
    use crate::authentication::MembershipWriter;

    #[derive(Default)]
    struct Writer {
        changes: Mutex<Vec<(String, bool)>>,
        revoke_on_assign: Option<Database>,
    }

    #[async_trait::async_trait]
    impl MembershipWriter for Writer {
        async fn role_membership(
            &self,
            tenant: &str,
            user: &str,
            role: &str,
            enabled: bool,
        ) -> Result<()> {
            ensure!(
                tenant == "acme" && user == "alice",
                "Unexpected membership scope"
            );
            self.changes
                .lock()
                .map_err(|_| anyhow::anyhow!("Membership test lock poisoned"))?
                .push((role.to_owned(), enabled));
            if enabled && let Some(database) = &self.revoke_on_assign {
                let mut transaction = database.tenant(tenant).await?;
                sqlx::query("DELETE FROM iam_user_roles WHERE tenant_id=$1 AND user_id=$2 AND role_name=$3").bind(tenant).bind(user).bind(role).execute(&mut *transaction).await?;
                transaction.commit().await?;
            }
            Ok(())
        }

        async fn tenant_membership(
            &self,
            tenant: &str,
            user: &str,
        ) -> Result<()> {
            ensure!(
                tenant == "acme" && user == "alice",
                "Unexpected tenant membership scope"
            );
            Ok(())
        }
    }

    #[tokio::test]
    async fn provider_role_sources_are_tenant_isolated_and_preserve_manual_grants()
    -> Result<()> {
        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'app'; CREATE ROLE galadril_auth LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'auth'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app; GRANT USAGE ON SCHEMA public TO galadril_auth;".to_vec()).with_tag("18.6-alpine").start().await?;
        let endpoint = format!(
            "{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        );
        let database = Database::connect(&format!(
            "postgres://galadril_app:app@{endpoint}"
        ))
        .await?;
        database.verify_security().await?;
        let store = PgFederationStore::new(database.clone());
        let iam = PgIamStore::new(database.clone());
        iam.create_user("acme", "alice", true).await?;
        iam.create_role("acme", "analyst").await?;
        iam.create_role("acme", "operator").await?;
        let mut transaction = database.tenant("acme").await?;
        sqlx::query("INSERT INTO sso_connections(domain,tenant_id,connection_id,enabled) VALUES('acme.example','acme','provider',true)").execute(&mut *transaction).await?;
        transaction.commit().await?;
        let config: FederationConfig = serde_json::from_value(
            json!({"connectionId":"provider","claim":"groups","enabled":true,"mappings":[{"externalGroup":"Finance","roleName":"analyst"}]}),
        )?;
        assert!(
            store.configure("acme", &config).await.is_err(),
            "Import requires an explicit tenant opt-in"
        );
        let mut transaction = database.tenant("acme").await?;
        sqlx::query("INSERT INTO sso_tenant_settings(tenant_id,permission_import) VALUES('acme',true)").execute(&mut *transaction).await?;
        transaction.commit().await?;
        store.configure("acme", &config).await?;
        assert!(store.configurations("other").await?.is_empty());
        assert!(store.configure("other", &config).await.is_err());
        let pool = sqlx::PgPool::connect(&format!(
            "postgres://galadril_auth:auth@{endpoint}"
        ))
        .await?;
        let writer = Arc::new(Writer::default());
        let authorization: Arc<dyn MembershipWriter> = writer.clone();
        let profile = json!({"groups":["Finance","administrator"]});
        assert!(
            federation::synchronize(
                &pool, None, "acme", "alice", "provider", &profile
            )
            .await
            .is_err()
        );
        assert!(iam.role_names_for_user("acme", "alice").await?.is_empty());
        federation::synchronize(
            &pool,
            Some(&authorization),
            "acme",
            "alice",
            "provider",
            &profile,
        )
        .await?;
        assert_eq!(
            iam.role_names_for_user("acme", "alice").await?,
            vec!["analyst"]
        );
        let mut disabled = config.clone();
        disabled.enabled = false;
        store.configure("acme", &disabled).await?;
        federation::synchronize(
            &pool,
            Some(&authorization),
            "acme",
            "alice",
            "provider",
            &json!({"groups":[]}),
        )
        .await?;
        assert_eq!(
            iam.role_names_for_user("acme", "alice").await?,
            vec!["analyst"],
            "Disabling synchronization must retain previously approved grants"
        );
        store.configure("acme", &config).await?;
        let mut transaction = database.tenant("acme").await?;
        sqlx::query("UPDATE sso_tenant_settings SET permission_import=false WHERE tenant_id='acme'").execute(&mut *transaction).await?;
        transaction.commit().await?;
        federation::synchronize(
            &pool,
            Some(&authorization),
            "acme",
            "alice",
            "provider",
            &json!({"groups":[]}),
        )
        .await?;
        assert_eq!(
            iam.role_names_for_user("acme", "alice").await?,
            vec!["analyst"],
            "Disabling permission import must stop changes without revoking grants"
        );
        let mut transaction = database.tenant("acme").await?;
        sqlx::query("UPDATE sso_tenant_settings SET permission_import=true WHERE tenant_id='acme'").execute(&mut *transaction).await?;
        transaction.commit().await?;
        assert!(
            federation::synchronize(
                &pool,
                Some(&authorization),
                "acme",
                "alice",
                "provider",
                &json!({})
            )
            .await
            .is_err()
        );
        assert_eq!(
            iam.role_names_for_user("acme", "alice").await?,
            vec!["analyst"]
        );
        assert!(
            iam.verify_role_unassignment("acme", "alice", "analyst")
                .await
                .is_err()
        );
        federation::synchronize(
            &pool,
            Some(&authorization),
            "acme",
            "alice",
            "provider",
            &json!({"groups":[]}),
        )
        .await?;
        assert!(iam.role_names_for_user("acme", "alice").await?.is_empty());
        federation::synchronize(
            &pool,
            Some(&authorization),
            "acme",
            "alice",
            "provider",
            &profile,
        )
        .await?;
        iam.assign_role_to_user("acme", "alice", "analyst").await?;
        iam.assign_role_to_user("acme", "alice", "operator").await?;
        federation::synchronize(
            &pool,
            Some(&authorization),
            "acme",
            "alice",
            "provider",
            &json!({"groups":[]}),
        )
        .await?;
        let roles = iam.role_names_for_user("acme", "alice").await?;
        assert!(roles.iter().any(|role| role == "analyst"));
        assert!(roles.iter().any(|role| role == "operator"));
        assert_eq!(
            writer
                .changes
                .lock()
                .map_err(|_| anyhow::anyhow!("Membership test lock poisoned"))?
                .iter()
                .filter(|(_, enabled)| !enabled)
                .count(),
            1
        );
        let revoking = Arc::new(Writer {
            changes: Mutex::default(),
            revoke_on_assign: Some(database.clone()),
        });
        let authorization: Arc<dyn MembershipWriter> = revoking.clone();
        assert!(
            federation::synchronize(
                &pool,
                Some(&authorization),
                "acme",
                "alice",
                "provider",
                &profile,
            )
            .await
            .is_err(),
            "A grant deleted during external projection must deny login"
        );
        assert_eq!(
            revoking
                .changes
                .lock()
                .map_err(|_| anyhow::anyhow!("Membership test lock poisoned"))?
                .last(),
            Some(&("analyst".to_owned(), false)),
            "Concurrent revocation must remove the stale SpiceDB membership"
        );
        pool.close().await;
        Ok(())
    }
}
