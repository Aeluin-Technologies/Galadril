//! PostgreSQL implementation of IAM persistence.
//!
//! SECURITY: Every query is explicitly tenant-scoped. Never write rows without
//! `tenant_id = $1` constraints.

use anyhow::{Context, Result};
use sqlx::Row;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::iam_store::IamStore;

pub struct PgIamStore {
    database: Database,
}

impl PgIamStore {
    pub fn new(database: Database) -> Self {
        Self { database }
    }
}

#[async_trait::async_trait]
impl IamStore for PgIamStore {
    async fn create_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            INSERT INTO iam_users (tenant_id, user_id, is_active)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, user_id) DO UPDATE
            SET is_active = EXCLUDED.is_active
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(is_active)
        .execute(&mut *tx)
        .await
        .context("Failed to upsert iam_users")?;
        tx.commit().await.context("Failed to commit create_user")?;
        Ok(())
    }

    async fn delete_user(&self, tenant_id: &str, user_id: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            DELETE FROM iam_users
            WHERE tenant_id = $1 AND user_id = $2
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .context("Failed to delete user from iam_users")?;
        tx.commit().await.context("Failed to commit delete_user")?;
        Ok(())
    }

    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            INSERT INTO iam_roles (tenant_id, role_name)
            VALUES ($1, $2)
            ON CONFLICT (tenant_id, role_name) DO NOTHING
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to insert iam_roles")?;
        tx.commit().await.context("Failed to commit create_role")?;
        Ok(())
    }

    async fn delete_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            DELETE FROM iam_roles
            WHERE tenant_id = $1 AND role_name = $2
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to delete role from iam_roles")?;
        tx.commit().await.context("Failed to commit delete_role")?;
        Ok(())
    }

    async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            r#"
            INSERT INTO iam_user_roles (tenant_id, user_id, role_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, user_id, role_name) DO NOTHING
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to insert iam_user_roles")?;
        tx.commit()
            .await
            .context("Failed to commit assign_role_to_user")?;
        Ok(())
    }

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
