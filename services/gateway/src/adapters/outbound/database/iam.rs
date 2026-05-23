//! PostgreSQL implementation of IAM persistence.
//!
//! SECURITY: Every query is explicitly tenant-scoped. Never write rows without
//! `tenant_id = $1` constraints.

use anyhow::{Context, Result};
use serde_json::Value;
use sqlx::{PgPool, Row};

use crate::application::ports::iam_store::IamStore;
use crate::domain::permission::{Effect, IamPermission};

pub struct PgIamStore {
    pool: PgPool,
}

impl PgIamStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    fn effect_as_str(effect: Effect) -> &'static str {
        match effect {
            Effect::Allow => "allow",
            Effect::Deny => "deny",
        }
    }

    fn parse_effect(s: &str) -> Result<Effect> {
        match s {
            "allow" => Ok(Effect::Allow),
            "deny" => Ok(Effect::Deny),
            other => anyhow::bail!("Unknown effect '{other}'"),
        }
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
        .execute(&self.pool)
        .await
        .context("Failed to upsert iam_users")?;
        Ok(())
    }

    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO iam_roles (tenant_id, role_name)
            VALUES ($1, $2)
            ON CONFLICT (tenant_id, role_name) DO NOTHING
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&self.pool)
        .await
        .context("Failed to insert iam_roles")?;
        Ok(())
    }

    async fn assign_role_to_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
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
        .execute(&self.pool)
        .await
        .context("Failed to insert iam_user_roles")?;
        Ok(())
    }

    async fn set_user_permissions(
        &self,
        tenant_id: &str,
        user_id: &str,
        permissions: &[IamPermission],
    ) -> Result<()> {
        let mut tx = self
            .pool
            .begin()
            .await
            .context("Failed to begin set_user_permissions tx")?;

        sqlx::query(
            r#"
            DELETE FROM iam_user_permissions
            WHERE tenant_id = $1 AND user_id = $2
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .context("Failed to delete existing iam_user_permissions")?;

        for p in permissions {
            sqlx::query(
                r#"
                INSERT INTO iam_user_permissions
                    (tenant_id, user_id, effect, action, scope, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                "#,
            )
            .bind(tenant_id)
            .bind(user_id)
            .bind(Self::effect_as_str(p.effect))
            .bind(&p.action)
            .bind(&p.scope)
            .execute(&mut *tx)
            .await
            .context("Failed to insert iam_user_permissions row")?;
        }

        tx.commit()
            .await
            .context("Failed to commit set_user_permissions tx")?;
        Ok(())
    }

    async fn set_role_permissions(
        &self,
        tenant_id: &str,
        role_name: &str,
        permissions: &[IamPermission],
    ) -> Result<()> {
        let mut tx = self
            .pool
            .begin()
            .await
            .context("Failed to begin set_role_permissions tx")?;

        sqlx::query(
            r#"
            DELETE FROM iam_role_permissions
            WHERE tenant_id = $1 AND role_name = $2
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .execute(&mut *tx)
        .await
        .context("Failed to delete existing iam_role_permissions")?;

        for p in permissions {
            sqlx::query(
                r#"
                INSERT INTO iam_role_permissions
                    (tenant_id, role_name, effect, action, scope, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                "#,
            )
            .bind(tenant_id)
            .bind(role_name)
            .bind(Self::effect_as_str(p.effect))
            .bind(&p.action)
            .bind(&p.scope)
            .execute(&mut *tx)
            .await
            .context("Failed to insert iam_role_permissions row")?;
        }

        tx.commit()
            .await
            .context("Failed to commit set_role_permissions tx")?;
        Ok(())
    }

    async fn get_effective_permissions_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<IamPermission>> {
        let rows = sqlx::query(
            r#"
            SELECT effect, action, scope
            FROM iam_user_permissions
            WHERE tenant_id = $1 AND user_id = $2

            UNION ALL

            SELECT rp.effect, rp.action, rp.scope
            FROM iam_role_permissions rp
            JOIN iam_user_roles ur
              ON ur.tenant_id = rp.tenant_id
             AND ur.role_name = rp.role_name
            WHERE ur.tenant_id = $1 AND ur.user_id = $2
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .fetch_all(&self.pool)
        .await
        .context("Failed to fetch effective permissions")?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let effect: String =
                row.try_get("effect").context("Missing effect")?;
            let action: String =
                row.try_get("action").context("Missing action")?;
            let scope: Value =
                row.try_get("scope").context("Missing scope")?;

            out.push(IamPermission {
                effect: Self::parse_effect(&effect)?,
                action,
                scope,
            });
        }

        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn effect_round_trip_strings_are_stable() {
        assert_eq!(PgIamStore::effect_as_str(Effect::Allow), "allow");
        assert!(PgIamStore::parse_effect("deny").is_ok());
        assert!(PgIamStore::parse_effect("nope").is_err());
    }
}
