//! PostgreSQL pool with transaction-local tenant isolation.

use std::str::FromStr;

use anyhow::{Context, Result, bail};
use sqlx::migrate::Migrator;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{PgPool, Postgres, Transaction};

use crate::domain::validate_tenant_id;

static GATEWAY_MIGRATOR: Migrator =
    sqlx::migrate!("../../schemas/postgres/gateway_migrations");

/// A shared pool that can only produce tenant-scoped transactions explicitly.
#[derive(Clone)]
pub struct Database {
    pool: PgPool,
}

impl Database {
    /// Connects the application pool.
    pub async fn connect(database_url: &str) -> Result<Self> {
        Self::connect_with_limit(database_url, 10).await
    }

    /// Connects a pool with an explicit limit, primarily for contention tests.
    pub async fn connect_with_limit(
        database_url: &str,
        max_connections: u32,
    ) -> Result<Self> {
        let options = PgConnectOptions::from_str(database_url)
            .context("Failed to parse database URL")?;
        let pool = PgPoolOptions::new()
            .max_connections(max_connections)
            .connect_with(options)
            .await
            .context("Failed to create PostgreSQL connection pool")?;
        GATEWAY_MIGRATOR
            .run(&pool)
            .await
            .context("Failed to apply Gateway PostgreSQL migrations")?;
        Ok(Self { pool })
    }

    /// Begins a transaction and installs tenant context before any query.
    pub async fn tenant(
        &self,
        tenant_id: &str,
    ) -> Result<Transaction<'static, Postgres>> {
        let tenant_id = validate_tenant_id(tenant_id)?;
        let mut transaction = self
            .pool
            .begin()
            .await
            .context("Failed to begin tenant transaction")?;
        sqlx::query("SELECT set_config('app.tenant_id', $1, true)")
            .bind(tenant_id)
            .execute(&mut *transaction)
            .await
            .context("Failed to set tenant context")?;
        Ok(transaction)
    }

    /// Exposes the unscoped pool only to security integration tests.
    #[cfg(test)]
    pub fn system(&self) -> &PgPool {
        &self.pool
    }

    /// Rejects an application identity or tenant table that can bypass RLS.
    pub async fn verify_security(&self) -> Result<()> {
        let unsafe_role: bool = sqlx::query_scalar(
            r#"
            SELECT role.rolsuper OR role.rolbypassrls
            FROM pg_roles AS role
            WHERE role.rolname = current_user
            "#,
        )
        .fetch_one(&self.pool)
        .await
        .context("Failed to inspect application role")?;
        if unsafe_role {
            bail!("Application role can bypass tenant RLS");
        }

        let insecure_tables: i64 = sqlx::query_scalar(
            r#"
            SELECT COUNT(*)
            FROM pg_class AS table_class
            JOIN pg_namespace AS namespace
              ON namespace.oid = table_class.relnamespace
            JOIN pg_attribute AS tenant_column
              ON tenant_column.attrelid = table_class.oid
             AND tenant_column.attname = 'tenant_id'
             AND NOT tenant_column.attisdropped
            WHERE namespace.nspname = 'public'
              AND table_class.relkind IN ('r', 'p')
              AND (NOT table_class.relrowsecurity OR NOT table_class.relforcerowsecurity)
            "#,
        )
        .fetch_one(&self.pool)
        .await
        .context("Failed to inspect tenant tables")?;
        if insecure_tables != 0 {
            bail!("Tenant tables must force RLS");
        }
        Ok(())
    }
}

/// Returns the tenant schema used by Apache AGE.
pub fn tenant_schema_name(tenant_id: &str) -> Result<String> {
    Ok(format!("tenant_{}", validate_tenant_id(tenant_id)?))
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use anyhow::{Context, Result};
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;
    use tokio::sync::Barrier;

    use super::{Database, GATEWAY_MIGRATOR, tenant_schema_name};

    const ROLE_SQL: &str = r#"
        CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS
            PASSWORD 'galadril_app';
        GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;
        CREATE TABLE platform_health (value INTEGER NOT NULL);
        INSERT INTO platform_health (value) VALUES (42);
        GRANT SELECT ON platform_health TO galadril_app;
    "#;
    const GATEWAY_SQL: &str = concat!(
        include_str!(
            "../../../../../../schemas/postgres/gateway_migrations/202608270001_iam.sql"
        ),
        include_str!(
            "../../../../../../schemas/postgres/gateway_migrations/202608270002_audit.sql"
        ),
        include_str!(
            "../../../../../../schemas/postgres/gateway_migrations/202608270003_conversations.sql"
        ),
        include_str!(
            "../../../../../../schemas/postgres/gateway_migrations/202608270004_pipelines.sql"
        ),
        include_str!(
            "../../../../../../schemas/postgres/gateway_migrations/202608270005_security.sql"
        ),
    );

    #[test]
    fn sqlx_owns_the_ordered_gateway_migration_history() {
        let versions = GATEWAY_MIGRATOR
            .iter()
            .map(|migration| migration.version)
            .collect::<Vec<_>>();

        assert_eq!(versions.len(), 5);
        assert!(versions.windows(2).all(|pair| {
            pair.first()
                .zip(pair.get(1))
                .is_some_and(|(left, right)| left < right)
        }));
    }

    #[test]
    fn derives_stable_age_schema() {
        assert!(matches!(
            tenant_schema_name("acme"),
            Ok(ref schema) if schema == "tenant_acme"
        ));
    }

    #[test]
    fn gateway_schema_defines_immutable_tenant_audit_history() {
        let normalized = GATEWAY_SQL.to_ascii_lowercase();
        assert!(
            normalized.contains("create table if not exists audit_events")
        );
        assert!(normalized.contains("force row level security"));
        assert!(normalized.contains("audit_events_immutable"));
        assert!(
            normalized.contains(
                "'grant select, insert on public.%i to galadril_app'"
            )
        );
        assert!(
            !normalized.contains(
                "grant select, insert, update, delete on audit_events"
            )
        );
    }

    #[test]
    fn gateway_schema_defines_tenant_isolated_conversation_history() {
        let normalized = GATEWAY_SQL.to_ascii_lowercase();
        for table in [
            "conversations",
            "conversation_messages",
            "conversation_message_revisions",
            "conversation_message_attachments",
        ] {
            assert!(
                normalized
                    .contains(&format!("create table if not exists {table}"))
            );
        }
        assert!(
            normalized.contains("conversation_message_revisions_immutable")
        );
        assert!(normalized.contains("active_generation_id"));
        assert!(normalized.contains("deleted_at"));
        assert!(normalized.contains("conversations_active_generation_fk"));
        assert!(normalized.contains("message_revisions_current_message_fk"));
    }

    #[test]
    fn gateway_schema_defines_versioned_pipeline_definitions() {
        let normalized = GATEWAY_SQL.to_ascii_lowercase();
        assert!(
            normalized
                .contains("create table if not exists pipeline_definitions")
        );
        assert!(
            normalized
                .contains("create table if not exists pipeline_revisions")
        );
        assert!(normalized.contains("pipeline_revisions_immutable"));
        assert!(normalized.contains("published_revision_id"));
        assert!(normalized.contains("pipeline_definitions_head_revision_fk"));
        assert!(normalized.contains("pipeline_revisions_parent_fk"));
    }

    #[tokio::test]
    async fn pool_context_is_transaction_local_under_contention() -> Result<()>
    {
        let container = Postgres::default()
            .with_init_sql(ROLE_SQL.as_bytes().to_vec())
            .with_tag("17.6-alpine")
            .start()
            .await
            .context("Failed to start PostgreSQL testcontainer")?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let database_url = format!(
            "postgres://galadril_app:galadril_app@{host}:{port}/postgres"
        );
        let database = Database::connect_with_limit(&database_url, 3).await?;
        database.verify_security().await?;

        for (tenant_id, user_id) in
            [("tenant_a", "user_a"), ("tenant_b", "user_b")]
        {
            let mut transaction = database.tenant(tenant_id).await?;
            sqlx::query(
                "INSERT INTO iam_users (tenant_id, user_id) VALUES ($1, $2)",
            )
            .bind(tenant_id)
            .bind(user_id)
            .execute(&mut *transaction)
            .await?;
            transaction.commit().await?;
        }

        let barrier = Arc::new(Barrier::new(3));
        let mut tasks = Vec::with_capacity(3);
        for (tenant_id, own_user, other_user) in [
            ("tenant_a", "user_a", "user_b"),
            ("tenant_b", "user_b", "user_a"),
        ] {
            let database = database.clone();
            let barrier = Arc::clone(&barrier);
            tasks.push(tokio::spawn(async move {
                let mut transaction = database.tenant(tenant_id).await?;
                barrier.wait().await;
                let current: String = sqlx::query_scalar(
                    "SELECT current_setting('app.tenant_id', true)",
                )
                .fetch_one(&mut *transaction)
                .await?;
                let own: i64 = sqlx::query_scalar(
                    "SELECT COUNT(*) FROM iam_users WHERE user_id = $1",
                )
                .bind(own_user)
                .fetch_one(&mut *transaction)
                .await?;
                let other: i64 = sqlx::query_scalar(
                    "SELECT COUNT(*) FROM iam_users WHERE user_id = $1",
                )
                .bind(other_user)
                .fetch_one(&mut *transaction)
                .await?;
                transaction.commit().await?;
                anyhow::ensure!(current == tenant_id);
                anyhow::ensure!(own == 1);
                anyhow::ensure!(other == 0);
                Result::<()>::Ok(())
            }));
        }

        let system_database = database.clone();
        let system_barrier = Arc::clone(&barrier);
        tasks.push(tokio::spawn(async move {
            system_barrier.wait().await;
            let tenant_rows: i64 =
                sqlx::query_scalar("SELECT COUNT(*) FROM iam_users")
                    .fetch_one(system_database.system())
                    .await?;
            let health: i32 =
                sqlx::query_scalar("SELECT value FROM platform_health")
                    .fetch_one(system_database.system())
                    .await?;
            anyhow::ensure!(tenant_rows == 0);
            anyhow::ensure!(health == 42);
            Result::<()>::Ok(())
        }));

        for task in tasks {
            task.await.context("Database isolation task failed")??;
        }

        // More operations than connections force deterministic connection
        // reuse.
        for index in 0..32 {
            let tenant_id = if index % 2 == 0 {
                "tenant_a"
            } else {
                "tenant_b"
            };
            let expected_user =
                if index % 2 == 0 { "user_a" } else { "user_b" };
            let mut transaction = database.tenant(tenant_id).await?;
            let users: Vec<String> =
                sqlx::query_scalar("SELECT user_id FROM iam_users")
                    .fetch_all(&mut *transaction)
                    .await?;
            transaction.commit().await?;
            anyhow::ensure!(users == [expected_user]);
        }
        Ok(())
    }

    #[tokio::test]
    async fn rls_rejects_cross_tenant_writes_without_leaking_context()
    -> Result<()> {
        let container = Postgres::default()
            .with_init_sql(ROLE_SQL.as_bytes().to_vec())
            .with_tag("17.6-alpine")
            .start()
            .await
            .context("Failed to start PostgreSQL testcontainer")?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let database = Database::connect_with_limit(
            &format!(
                "postgres://galadril_app:galadril_app@{host}:{port}/postgres"
            ),
            1,
        )
        .await?;

        for (tenant_id, user_id) in
            [("tenant_a", "user_a"), ("tenant_b", "user_b")]
        {
            let mut transaction = database.tenant(tenant_id).await?;
            sqlx::query(
                "INSERT INTO iam_users (tenant_id, user_id) VALUES ($1, $2)",
            )
            .bind(tenant_id)
            .bind(user_id)
            .execute(&mut *transaction)
            .await?;
            transaction.commit().await?;
        }

        let mut transaction = database.tenant("tenant_a").await?;
        let updated = sqlx::query(
            "UPDATE iam_users SET is_active = FALSE WHERE user_id = 'user_b'",
        )
        .execute(&mut *transaction)
        .await?;
        let deleted =
            sqlx::query("DELETE FROM iam_users WHERE user_id = 'user_b'")
                .execute(&mut *transaction)
                .await?;
        anyhow::ensure!(updated.rows_affected() == 0);
        anyhow::ensure!(deleted.rows_affected() == 0);
        transaction.commit().await?;

        let mut transaction = database.tenant("tenant_a").await?;
        let forged = sqlx::query(
            "INSERT INTO iam_users (tenant_id, user_id) VALUES ('tenant_b', 'forged')",
        )
        .execute(&mut *transaction)
        .await;
        anyhow::ensure!(forged.is_err());
        transaction.rollback().await?;

        let mut transaction = database.tenant("tenant_a").await?;
        let moved = sqlx::query(
            "UPDATE iam_users SET tenant_id = 'tenant_b' WHERE user_id = 'user_a'",
        )
        .execute(&mut *transaction)
        .await;
        anyhow::ensure!(moved.is_err());
        transaction.rollback().await?;

        for (tenant_id, expected_user) in
            [("tenant_a", "user_a"), ("tenant_b", "user_b")]
        {
            let mut transaction = database.tenant(tenant_id).await?;
            let users: Vec<String> =
                sqlx::query_scalar("SELECT user_id FROM iam_users")
                    .fetch_all(&mut *transaction)
                    .await?;
            transaction.commit().await?;
            anyhow::ensure!(users == [expected_user]);
        }

        let visible: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM iam_users")
                .fetch_one(database.system())
                .await?;
        anyhow::ensure!(visible == 0);
        Ok(())
    }

    #[tokio::test]
    async fn audit_history_is_immutable_and_tenant_isolated() -> Result<()> {
        let container = Postgres::default()
            .with_init_sql(ROLE_SQL.as_bytes().to_vec())
            .with_tag("17.6-alpine")
            .start()
            .await
            .context("Failed to start PostgreSQL testcontainer")?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let database = Database::connect_with_limit(
            &format!(
                "postgres://galadril_app:galadril_app@{host}:{port}/postgres"
            ),
            1,
        )
        .await?;

        for (tenant_id, audit_id, actor_id) in [
            ("tenant_a", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "user_a"),
            ("tenant_b", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "user_b"),
        ] {
            let mut transaction = database.tenant(tenant_id).await?;
            sqlx::query(
                r#"
                INSERT INTO audit_events (
                    tenant_id, audit_id, operation_id, actor_type, actor_id,
                    action, resource_type, resource_id, outcome, request_id
                ) VALUES ($1, $2, $2, 'user', $3, 'create_user', 'user', $3,
                          'succeeded', $2)
                "#,
            )
            .bind(tenant_id)
            .bind(audit_id)
            .bind(actor_id)
            .execute(&mut *transaction)
            .await?;
            transaction.commit().await?;
        }

        let mut transaction = database.tenant("tenant_a").await?;
        let actors: Vec<String> =
            sqlx::query_scalar("SELECT actor_id FROM audit_events")
                .fetch_all(&mut *transaction)
                .await?;
        anyhow::ensure!(actors == ["user_a"]);
        transaction.commit().await?;

        let mut transaction = database.tenant("tenant_a").await?;
        let forged = sqlx::query(
            r#"
            INSERT INTO audit_events (
                tenant_id, audit_id, operation_id, actor_type, actor_id,
                action, resource_type, resource_id, outcome, request_id
            ) VALUES ('tenant_b', 'cccccccccccccccccccccccccccccccc',
                      'cccccccccccccccccccccccccccccccc', 'user', 'user_a',
                      'create_user', 'user', 'forged', 'succeeded', 'request')
            "#,
        )
        .execute(&mut *transaction)
        .await;
        anyhow::ensure!(forged.is_err());
        transaction.rollback().await?;

        for statement in [
            "UPDATE audit_events SET outcome = 'failed'",
            "DELETE FROM audit_events",
        ] {
            let mut transaction = database.tenant("tenant_a").await?;
            let mutation =
                sqlx::query(statement).execute(&mut *transaction).await;
            anyhow::ensure!(mutation.is_err());
            transaction.rollback().await?;
        }

        let visible: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM audit_events")
                .fetch_one(database.system())
                .await?;
        anyhow::ensure!(visible == 0);
        Ok(())
    }

    #[tokio::test]
    async fn conversation_and_pipeline_history_remain_tenant_isolated()
    -> Result<()> {
        let container = Postgres::default()
            .with_init_sql(ROLE_SQL.as_bytes().to_vec())
            .with_tag("17.6-alpine")
            .start()
            .await
            .context("Failed to start PostgreSQL testcontainer")?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let database = Database::connect_with_limit(
            &format!(
                "postgres://galadril_app:galadril_app@{host}:{port}/postgres"
            ),
            1,
        )
        .await?;

        for (tenant_id, marker) in [("tenant_a", 'a'), ("tenant_b", 'b')] {
            let conversation_id = marker.to_string().repeat(32);
            let message_id =
                marker.to_ascii_uppercase().to_string().repeat(32);
            let revision_id = marker.to_string().repeat(32);
            let mut transaction = database.tenant(tenant_id).await?;
            sqlx::query(
                r#"
                INSERT INTO conversations (
                    tenant_id, conversation_id, owner_id, title
                ) VALUES ($1, $2, $3, 'Tenant conversation')
                "#,
            )
            .bind(tenant_id)
            .bind(&conversation_id)
            .bind(format!("user_{marker}"))
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r#"
                INSERT INTO conversation_messages (
                    tenant_id, conversation_id, message_id, role, content,
                    created_by
                ) VALUES ($1, $2, $3, 'user', 'hello', $4)
                "#,
            )
            .bind(tenant_id)
            .bind(&conversation_id)
            .bind(&message_id)
            .bind(format!("user_{marker}"))
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r#"
                INSERT INTO conversation_message_revisions (
                    tenant_id, conversation_id, message_id, revision,
                    content, status, changed_by
                ) VALUES ($1, $2, $3, 1, 'hello', 'completed', $4)
                "#,
            )
            .bind(tenant_id)
            .bind(&conversation_id)
            .bind(&message_id)
            .bind(format!("user_{marker}"))
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r#"
                INSERT INTO pipeline_definitions (
                    tenant_id, pipeline_id, name, owner_id, head_revision_id
                ) VALUES ($1, 'daily', 'Daily', $2, $3)
                "#,
            )
            .bind(tenant_id)
            .bind(format!("user_{marker}"))
            .bind(&revision_id)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r#"
                INSERT INTO pipeline_revisions (
                    tenant_id, pipeline_id, revision_id, definition,
                    author_id, message
                ) VALUES ($1, 'daily', $2, '{}', $3, 'root')
                "#,
            )
            .bind(tenant_id)
            .bind(&revision_id)
            .bind(format!("user_{marker}"))
            .execute(&mut *transaction)
            .await?;
            transaction.commit().await?;
        }

        let mut transaction = database.tenant("tenant_a").await?;
        let conversation_count: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM conversations")
                .fetch_one(&mut *transaction)
                .await?;
        let message_count: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM conversation_message_revisions",
        )
        .fetch_one(&mut *transaction)
        .await?;
        let pipeline_count: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM pipeline_revisions")
                .fetch_one(&mut *transaction)
                .await?;
        let cross_tenant_update = sqlx::query(
            "UPDATE pipeline_definitions SET name = 'forged' WHERE tenant_id = 'tenant_b'",
        )
        .execute(&mut *transaction)
        .await?;
        anyhow::ensure!(conversation_count == 1);
        anyhow::ensure!(message_count == 1);
        anyhow::ensure!(pipeline_count == 1);
        anyhow::ensure!(cross_tenant_update.rows_affected() == 0);
        transaction.commit().await?;

        for statement in [
            "UPDATE conversation_message_revisions SET content = 'changed'",
            "DELETE FROM pipeline_revisions",
        ] {
            let mut transaction = database.tenant("tenant_a").await?;
            let mutation =
                sqlx::query(statement).execute(&mut *transaction).await;
            anyhow::ensure!(mutation.is_err());
            transaction.rollback().await?;
        }

        use crate::adapters::outbound::database::pipelines::PgPipelineStore;
        use crate::application::ports::pipeline_store::PipelineStore;
        let store = PgPipelineStore::new(database.clone());
        let revision_a = "a".repeat(32);
        let revision_b = "b".repeat(32);
        anyhow::ensure!(
            store
                .publish("tenant_a", "daily", &revision_b)
                .await
                .is_err()
        );
        let published =
            store.publish("tenant_a", "daily", &revision_a).await?;
        anyhow::ensure!(
            published.published_revision_id.as_deref() ==
                Some(revision_a.as_str())
        );
        anyhow::ensure!(
            store
                .get("tenant_b", "daily")
                .await?
                .context("missing tenant B")?
                .published_revision_id
                .is_none()
        );
        anyhow::ensure!(
            store
                .delete("tenant_a", "daily", &revision_b)
                .await
                .is_err()
        );
        anyhow::ensure!(
            store
                .get("tenant_a", "daily")
                .await?
                .context("missing tenant A")?
                .published_revision_id
                .is_some()
        );
        store.delete("tenant_a", "daily", &revision_a).await?;
        anyhow::ensure!(store.get("tenant_a", "daily").await?.is_none());
        let mut retired = database.tenant("tenant_a").await?;
        let active: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM pipeline_definitions WHERE published_revision_id IS NOT NULL AND deleted_at IS NULL").fetch_one(&mut *retired).await?;
        anyhow::ensure!(active == 0);
        retired.commit().await?;

        let visible_conversations: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM conversations")
                .fetch_one(database.system())
                .await?;
        let visible_pipelines: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM pipeline_definitions")
                .fetch_one(database.system())
                .await?;
        anyhow::ensure!(visible_conversations == 0);
        anyhow::ensure!(visible_pipelines == 0);
        Ok(())
    }
}
