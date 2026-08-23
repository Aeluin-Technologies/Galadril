//! PostgreSQL implementation of the UserDirectory port.

use anyhow::{Context, Result};
use sqlx::Row;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::user_directory::{UserDirectory, UserStatus};

pub struct PgUserDirectory {
    database: Database,
}

impl PgUserDirectory {
    pub fn new(database: Database) -> Self {
        Self { database }
    }
}

#[async_trait::async_trait]
impl UserDirectory for PgUserDirectory {
    async fn get_user_status(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<UserStatus> {
        // Expected table
        // iam_users(tenant_id text, user_id text, is_active bool, primary key
        // (tenant_id, user_id))
        let mut tx = self.database.tenant(tenant_id).await?;
        let row = sqlx::query(
            r#"
            SELECT is_active
            FROM iam_users
            WHERE tenant_id = $1 AND user_id = $2
            LIMIT 1
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .fetch_optional(&mut *tx)
        .await
        .context("Failed to query user directory")?;
        tx.commit()
            .await
            .context("Failed to commit user directory read")?;

        let Some(row) = row else {
            return Ok(UserStatus::NotFound);
        };

        let is_active: bool =
            row.try_get("is_active").context("Missing is_active")?;
        Ok(if is_active {
            UserStatus::Active
        } else {
            UserStatus::Disabled
        })
    }
}
