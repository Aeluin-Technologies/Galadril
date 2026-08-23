//! PostgreSQL pool with transaction-local tenant isolation.

use std::str::FromStr;

use anyhow::{Context, Result, bail};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{PgPool, Postgres, Transaction};

const MAX_TENANT_ID_LEN: usize = 64;

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

    /// Exposes an unscoped pool only for explicitly global tables and checks.
    pub fn system(&self) -> &PgPool {
        &self.pool
    }

    /// Rejects an application identity or tenant table that can bypass RLS.
    pub async fn verify_security(&self) -> Result<()> {
        let unsafe_role: bool = sqlx::query_scalar(
            r#"
            SELECT role.rolsuper OR role.rolbypassrls OR EXISTS (
                SELECT 1
                FROM pg_class AS table_class
                JOIN pg_namespace AS namespace
                  ON namespace.oid = table_class.relnamespace
                JOIN pg_attribute AS tenant_column
                  ON tenant_column.attrelid = table_class.oid
                 AND tenant_column.attname = 'tenant_id'
                 AND NOT tenant_column.attisdropped
                WHERE namespace.nspname = 'public'
                  AND table_class.relkind IN ('r', 'p')
                  AND table_class.relowner = role.oid
            )
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

/// Validates and normalizes an externally resolved tenant identifier.
pub fn validate_tenant_id(tenant_id: &str) -> Result<&str> {
    let tenant_id = tenant_id.trim();
    if tenant_id.is_empty() {
        bail!("tenant_id is empty");
    }
    if tenant_id.len() > MAX_TENANT_ID_LEN {
        bail!("tenant_id is too long");
    }
    if !tenant_id.bytes().all(|byte| {
        byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
    }) {
        bail!("tenant_id contains invalid characters");
    }
    Ok(tenant_id)
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

    use super::{Database, tenant_schema_name, validate_tenant_id};

    const ROLE_SQL: &str = r#"
        CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS
            PASSWORD 'galadril_app';
        CREATE TABLE platform_health (value INTEGER NOT NULL);
        INSERT INTO platform_health (value) VALUES (42);
        GRANT SELECT ON platform_health TO galadril_app;
    "#;
    const GATEWAY_SQL: &str =
        include_str!("../../../../../../schemas/postgres/gateway.sql");

    #[test]
    fn validates_tenant_identifiers() {
        assert!(matches!(validate_tenant_id(" acme "), Ok("acme")));
        assert!(validate_tenant_id("").is_err());
        assert!(validate_tenant_id("evil;drop").is_err());
        assert!(validate_tenant_id("a/b").is_err());
        assert!(validate_tenant_id(&"a".repeat(65)).is_err());
    }

    #[test]
    fn derives_stable_age_schema() {
        assert!(matches!(
            tenant_schema_name("acme"),
            Ok(ref schema) if schema == "tenant_acme"
        ));
    }

    #[tokio::test]
    async fn pool_context_is_transaction_local_under_contention() -> Result<()>
    {
        let container = Postgres::default()
            .with_init_sql(ROLE_SQL.as_bytes().to_vec())
            .with_init_sql(GATEWAY_SQL.as_bytes().to_vec())
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
            .with_init_sql(GATEWAY_SQL.as_bytes().to_vec())
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
}
