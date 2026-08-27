//! PostgreSQL implementation of IAM persistence.
//!
//! SECURITY: Every query is explicitly tenant-scoped. Never write rows without
//! `tenant_id = $1` constraints.

use anyhow::{Context, Result, bail};
use sqlx::Row;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::iam_store::{
    IamRole, IamStore, IamUser, RoleAssignment,
};

const HARD_LIMIT: usize = 100;

pub struct PgIamStore {
    database: Database,
}

impl PgIamStore {
    /// Creates an IAM store over the shared RLS-aware database pool.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Converts PostgreSQL timestamps to Unix milliseconds.
    fn to_ms(value: sqlx::types::time::OffsetDateTime) -> i64 {
        value.unix_timestamp() * 1000 +
            i64::from(value.nanosecond()) / 1_000_000
    }
}

#[async_trait::async_trait]
impl IamStore for PgIamStore {
    /// Creates or reconfigures a live tenant user without reviving tombstones.
    async fn create_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let result = sqlx::query(
            r#"
            INSERT INTO iam_users (tenant_id, user_id, is_active)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, user_id) DO UPDATE
            SET is_active = EXCLUDED.is_active, updated_at = NOW()
            WHERE iam_users.deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(is_active)
        .execute(&mut *tx)
        .await
        .context("Failed to upsert iam_users")?;
        if result.rows_affected() != 1 {
            bail!("User identifier is tombstoned and cannot be reused");
        }
        tx.commit().await.context("Failed to commit create_user")?;
        Ok(())
    }

    /// Changes the active state of one live tenant user.
    async fn update_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let result = sqlx::query(
            r#"
            UPDATE iam_users
            SET is_active = $3, updated_at = NOW()
            WHERE tenant_id = $1 AND user_id = $2 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(is_active)
        .execute(&mut *tx)
        .await
        .context("Failed to update iam_user")?;
        if result.rows_affected() != 1 {
            bail!("User is unavailable");
        }
        tx.commit().await.context("Failed to commit update_user")
    }

    /// Tombstones a tenant user and removes durable role assignments.
    async fn delete_user(&self, tenant_id: &str, user_id: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            "DELETE FROM iam_user_roles WHERE tenant_id = $1 AND user_id = $2",
        )
        .bind(tenant_id)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .context("Failed to remove deleted user assignments")?;
        let result = sqlx::query(
            r#"
            UPDATE iam_users
            SET is_active = FALSE, deleted_at = NOW(), updated_at = NOW()
            WHERE tenant_id = $1 AND user_id = $2 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .context("Failed to soft-delete iam_user")?;
        if result.rows_affected() != 1 {
            bail!("User is unavailable");
        }
        tx.commit().await.context("Failed to commit delete_user")
    }

    /// Creates a live tenant role without reviving tombstoned names.
    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let result = sqlx::query(
            r#"
            INSERT INTO iam_roles (tenant_id, role_name)
            VALUES ($1, $2)
            ON CONFLICT (tenant_id, role_name) DO UPDATE
            SET updated_at = NOW()
            WHERE iam_roles.deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to insert iam_roles")?;
        if result.rows_affected() != 1 {
            bail!("Role identifier is tombstoned and cannot be reused");
        }
        tx.commit().await.context("Failed to commit create_role")?;
        Ok(())
    }

    /// Tombstones a tenant role and removes durable user assignments.
    async fn delete_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            "DELETE FROM iam_user_roles WHERE tenant_id = $1 AND role_name = $2",
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to remove deleted role assignments")?;
        let result = sqlx::query(
            r#"
            UPDATE iam_roles
            SET deleted_at = NOW(), updated_at = NOW()
            WHERE tenant_id = $1 AND role_name = $2 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to soft-delete iam_role")?;
        if result.rows_affected() != 1 {
            bail!("Role is unavailable");
        }
        tx.commit().await.context("Failed to commit delete_role")
    }

    /// Persists one assignment only when both live records exist.
    async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let result = sqlx::query(
            r#"
            INSERT INTO iam_user_roles (tenant_id, user_id, role_name)
            SELECT $1, $2, $3
            WHERE EXISTS (
                SELECT 1 FROM iam_users
                WHERE tenant_id = $1 AND user_id = $2
                  AND is_active = TRUE AND deleted_at IS NULL
            ) AND EXISTS (
                SELECT 1 FROM iam_roles
                WHERE tenant_id = $1 AND role_name = $3
                  AND deleted_at IS NULL
            )
            ON CONFLICT (tenant_id, user_id, role_name) DO NOTHING
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to insert iam_user_roles")?;
        if result.rows_affected() != 1 {
            bail!("Active user and role are required for assignment");
        }
        tx.commit()
            .await
            .context("Failed to commit assign_role_to_user")?;
        Ok(())
    }

    /// Removes one durable tenant user-to-role assignment.
    async fn unassign_role_from_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            DELETE FROM iam_user_roles
            WHERE tenant_id = $1 AND user_id = $2 AND role_name = $3
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to delete iam_user_roles assignment")?;
        tx.commit()
            .await
            .context("Failed to commit unassign_role_from_user")?;
        Ok(())
    }

    /// Lists current tenant users with a hard result bound.
    async fn list_users(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<IamUser>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT user_id, is_active, created_at, updated_at
            FROM iam_users
            WHERE tenant_id = $1 AND deleted_at IS NULL
            ORDER BY user_id
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list iam_users")?;
        tx.commit()
            .await
            .context("Failed to commit iam_users read")?;

        let mut users = Vec::with_capacity(rows.len());
        for row in rows {
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            users.push(IamUser {
                user_id: row.try_get("user_id")?,
                is_active: row.try_get("is_active")?,
                created_at_ms: Self::to_ms(created_at),
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(users)
    }

    /// Lists current tenant roles with a hard result bound.
    async fn list_roles(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<IamRole>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT role_name, created_at, updated_at
            FROM iam_roles
            WHERE tenant_id = $1 AND deleted_at IS NULL
            ORDER BY role_name
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list iam_roles")?;
        tx.commit()
            .await
            .context("Failed to commit iam_roles read")?;

        let mut roles = Vec::with_capacity(rows.len());
        for row in rows {
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            roles.push(IamRole {
                role_name: row.try_get("role_name")?,
                created_at_ms: Self::to_ms(created_at),
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(roles)
    }

    /// Lists assignments whose user and role remain live.
    async fn list_role_assignments(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<RoleAssignment>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT assignment.user_id, assignment.role_name,
                   assignment.created_at
            FROM iam_user_roles AS assignment
            JOIN iam_users AS tenant_user
              ON tenant_user.tenant_id = assignment.tenant_id
             AND tenant_user.user_id = assignment.user_id
             AND tenant_user.deleted_at IS NULL
            JOIN iam_roles AS tenant_role
              ON tenant_role.tenant_id = assignment.tenant_id
             AND tenant_role.role_name = assignment.role_name
             AND tenant_role.deleted_at IS NULL
            WHERE assignment.tenant_id = $1
            ORDER BY user_id, role_name
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list iam_user_roles")?;
        tx.commit()
            .await
            .context("Failed to commit iam_user_roles read")?;

        let mut assignments = Vec::with_capacity(rows.len());
        for row in rows {
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            assignments.push(RoleAssignment {
                user_id: row.try_get("user_id")?,
                role_name: row.try_get("role_name")?,
                created_at_ms: Self::to_ms(created_at),
            });
        }
        Ok(assignments)
    }

    /// Lists current role names for authorization cleanup.
    async fn role_names_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let roles = sqlx::query_scalar(
            r#"
            SELECT assignment.role_name
            FROM iam_user_roles AS assignment
            JOIN iam_roles AS role
              ON role.tenant_id = assignment.tenant_id
             AND role.role_name = assignment.role_name
             AND role.deleted_at IS NULL
            WHERE assignment.tenant_id = $1 AND assignment.user_id = $2
            ORDER BY assignment.role_name
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list user roles for cleanup")?;
        tx.commit().await?;
        Ok(roles)
    }

    /// Lists current role members for authorization cleanup.
    async fn user_ids_for_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<Vec<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let users = sqlx::query_scalar(
            r#"
            SELECT assignment.user_id
            FROM iam_user_roles AS assignment
            JOIN iam_users AS tenant_user
              ON tenant_user.tenant_id = assignment.tenant_id
             AND tenant_user.user_id = assignment.user_id
             AND tenant_user.deleted_at IS NULL
            WHERE assignment.tenant_id = $1 AND assignment.role_name = $2
            ORDER BY assignment.user_id
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list role users for cleanup")?;
        tx.commit().await?;
        Ok(users)
    }

    /// Concatenates active tenant policies in deterministic identifier order.
    async fn get_active_cedar_policies(
        &self,
        tenant_id: &str,
    ) -> Result<Option<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT content
            FROM auth_policies
            WHERE tenant_id = $1 AND is_active = TRUE
            ORDER BY id
            "#,
        )
        .bind(tenant_id)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to load active Cedar policies")?;
        tx.commit()
            .await
            .context("Failed to commit Cedar policy read")?;
        if rows.is_empty() {
            return Ok(None);
        }
        let mut combined = String::new();
        for row in rows {
            let content: String = row.try_get("content")?;
            combined.push_str(&content);
            combined.push('\n');
        }
        Ok(Some(combined))
    }

    /// Validates persistence scope and upserts one Cedar policy document.
    async fn upsert_cedar_policy(
        &self,
        tenant_id: &str,
        policy_id: &str,
        content: &str,
        is_active: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            INSERT INTO auth_policies (tenant_id, id, content, is_active)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, id) DO UPDATE
            SET content = EXCLUDED.content,
                is_active = EXCLUDED.is_active,
                updated_at = NOW()
            "#,
        )
        .bind(tenant_id)
        .bind(policy_id)
        .bind(content)
        .bind(is_active)
        .execute(&mut *tx)
        .await
        .context("Failed to upsert Cedar policy")?;
        tx.commit()
            .await
            .context("Failed to commit Cedar policy update")?;
        Ok(())
    }
}
