//! PostgreSQL adapter for versioned tenant conversations and messages.

use std::collections::HashMap;

use anyhow::{Context, Result, bail};
use sqlx::types::time::OffsetDateTime;
use sqlx::{FromRow, Postgres, Transaction};

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::conversation_store::{
    AttachmentKind, Conversation, ConversationMessage, ConversationStore,
    MessageAttachment, MessageRole, MessageStatus, NewConversation,
    NewConversationMessage,
};

#[derive(FromRow)]
struct ConversationRow {
    conversation_id: String,
    owner_id: String,
    title: String,
    revision: i64,
    active_generation_id: Option<String>,
    created_at: OffsetDateTime,
    updated_at: OffsetDateTime,
    deleted_at: Option<OffsetDateTime>,
}

#[derive(FromRow)]
struct MessageRow {
    message_id: String,
    role: String,
    content: String,
    model_alias: Option<String>,
    status: String,
    revision: i64,
    created_by: String,
    created_at: OffsetDateTime,
    updated_at: OffsetDateTime,
    deleted_at: Option<OffsetDateTime>,
}

#[derive(FromRow)]
struct AttachmentRow {
    message_id: String,
    object_key: String,
    kind: String,
    file_name: Option<String>,
    content_type: Option<String>,
    size_bytes: Option<i64>,
}

/// SQL-backed conversation store whose every operation opens an RLS
/// transaction.
pub struct PgConversationStore {
    database: Database,
}

impl PgConversationStore {
    /// Creates a store over the shared, security-verified connection pool.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Converts PostgreSQL timestamps without truncating before milliseconds.
    fn to_ms(value: OffsetDateTime) -> i64 {
        value.unix_timestamp_nanos() as i64 / 1_000_000
    }

    /// Maps one database conversation row into the application projection.
    fn map_conversation(row: ConversationRow) -> Conversation {
        Conversation {
            conversation_id: row.conversation_id,
            owner_id: row.owner_id,
            title: row.title,
            revision: row.revision,
            active_generation_id: row.active_generation_id,
            messages: Vec::new(),
            created_at_ms: Self::to_ms(row.created_at),
            updated_at_ms: Self::to_ms(row.updated_at),
            deleted_at_ms: row.deleted_at.map(Self::to_ms),
        }
    }

    /// Maps one database message and its already grouped attachments.
    fn map_message(
        row: MessageRow,
        attachments: Vec<MessageAttachment>,
    ) -> Result<ConversationMessage> {
        Ok(ConversationMessage {
            message_id: row.message_id,
            role: MessageRole::parse(&row.role)?,
            content: row.content,
            model_alias: row.model_alias,
            status: MessageStatus::parse(&row.status)?,
            revision: row.revision,
            created_by: row.created_by,
            attachments,
            created_at_ms: Self::to_ms(row.created_at),
            updated_at_ms: Self::to_ms(row.updated_at),
            deleted_at_ms: row.deleted_at.map(Self::to_ms),
        })
    }

    /// Inserts attachment references inside the caller's existing transaction.
    async fn insert_attachments(
        transaction: &mut Transaction<'static, Postgres>,
        tenant_id: &str,
        conversation_id: &str,
        message_id: &str,
        attachments: &[MessageAttachment],
    ) -> Result<()> {
        for attachment in attachments {
            sqlx::query(
                r#"
                INSERT INTO conversation_message_attachments (
                    tenant_id, conversation_id, message_id, object_key, kind,
                    file_name, content_type, size_bytes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                "#,
            )
            .bind(tenant_id)
            .bind(conversation_id)
            .bind(message_id)
            .bind(&attachment.object_key)
            .bind(attachment.kind.as_str())
            .bind(&attachment.file_name)
            .bind(&attachment.content_type)
            .bind(attachment.size_bytes)
            .execute(&mut **transaction)
            .await
            .context("Failed to persist conversation attachment")?;
        }
        Ok(())
    }

    /// Appends the current message state to immutable revision history.
    async fn append_message_revision(
        transaction: &mut Transaction<'static, Postgres>,
        tenant_id: &str,
        conversation_id: &str,
        message_id: &str,
        changed_by: &str,
    ) -> Result<()> {
        let inserted = sqlx::query(
            r#"
            INSERT INTO conversation_message_revisions (
                tenant_id, conversation_id, message_id, revision, content,
                status, attachments, deleted_at, changed_by
            )
            SELECT message.tenant_id, message.conversation_id,
                   message.message_id, message.revision, message.content,
                   message.status,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'object_key', attachment.object_key,
                           'kind', attachment.kind,
                           'file_name', attachment.file_name,
                           'content_type', attachment.content_type,
                           'size_bytes', attachment.size_bytes
                       ) ORDER BY attachment.object_key)
                       FROM conversation_message_attachments AS attachment
                       WHERE attachment.tenant_id = message.tenant_id
                         AND attachment.conversation_id = message.conversation_id
                         AND attachment.message_id = message.message_id
                   ), '[]'::jsonb),
                   message.deleted_at, $4
            FROM conversation_messages AS message
            WHERE message.tenant_id = $1
              AND message.conversation_id = $2
              AND message.message_id = $3
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(message_id)
        .bind(changed_by)
        .execute(&mut **transaction)
        .await
        .context("Failed to append immutable message revision")?;
        if inserted.rows_affected() != 1 {
            bail!("Conversation message is unavailable");
        }
        Ok(())
    }

    /// Loads all attachment rows for a conversation in one bounded query.
    async fn load_attachments(
        transaction: &mut Transaction<'static, Postgres>,
        tenant_id: &str,
        conversation_id: &str,
    ) -> Result<HashMap<String, Vec<MessageAttachment>>> {
        let rows = sqlx::query_as::<_, AttachmentRow>(
            r#"
            SELECT message_id, object_key, kind, file_name, content_type,
                   size_bytes
            FROM conversation_message_attachments
            WHERE tenant_id = $1 AND conversation_id = $2
            ORDER BY message_id, object_key
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .fetch_all(&mut **transaction)
        .await
        .context("Failed to load conversation attachments")?;
        let mut grouped = HashMap::new();
        for row in rows {
            grouped.entry(row.message_id).or_insert_with(Vec::new).push(
                MessageAttachment {
                    object_key: row.object_key,
                    kind: AttachmentKind::parse(&row.kind)?,
                    file_name: row.file_name,
                    content_type: row.content_type,
                    size_bytes: row.size_bytes,
                },
            );
        }
        Ok(grouped)
    }

    /// Loads one conversation within a transaction that already has RLS
    /// context.
    async fn load_conversation(
        transaction: &mut Transaction<'static, Postgres>,
        tenant_id: &str,
        conversation_id: &str,
        include_deleted_messages: bool,
    ) -> Result<Option<Conversation>> {
        let row = sqlx::query_as::<_, ConversationRow>(
            r#"
            SELECT conversation_id, owner_id, title, revision,
                   active_generation_id, created_at, updated_at, deleted_at
            FROM conversations
            WHERE tenant_id = $1 AND conversation_id = $2
              AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .fetch_optional(&mut **transaction)
        .await
        .context("Failed to load conversation")?;
        let Some(row) = row else {
            return Ok(None);
        };

        let message_rows = sqlx::query_as::<_, MessageRow>(
            r#"
            SELECT message_id, role, content, model_alias, status, revision,
                   created_by, created_at, updated_at, deleted_at
            FROM conversation_messages
            WHERE tenant_id = $1 AND conversation_id = $2
              AND ($3 OR deleted_at IS NULL)
            ORDER BY created_at, message_id
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(include_deleted_messages)
        .fetch_all(&mut **transaction)
        .await
        .context("Failed to load conversation messages")?;
        let mut attachments =
            Self::load_attachments(transaction, tenant_id, conversation_id)
                .await?;
        let mut conversation = Self::map_conversation(row);
        conversation.messages.reserve(message_rows.len());
        for message in message_rows {
            let message_attachments =
                attachments.remove(&message.message_id).unwrap_or_default();
            conversation
                .messages
                .push(Self::map_message(message, message_attachments)?);
        }
        Ok(Some(conversation))
    }

    /// Inserts one message and its initial immutable revision.
    async fn insert_message(
        transaction: &mut Transaction<'static, Postgres>,
        tenant_id: &str,
        conversation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO conversation_messages (
                tenant_id, conversation_id, message_id, role, content,
                model_alias, status, created_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(message.message_id)
        .bind(message.role.as_str())
        .bind(message.content)
        .bind(message.model_alias)
        .bind(message.status.as_str())
        .bind(message.created_by)
        .execute(&mut **transaction)
        .await
        .context("Failed to insert conversation message")?;
        Self::insert_attachments(
            transaction,
            tenant_id,
            conversation_id,
            message.message_id,
            message.attachments,
        )
        .await?;
        Self::append_message_revision(
            transaction,
            tenant_id,
            conversation_id,
            message.message_id,
            message.created_by,
        )
        .await
    }
}

#[async_trait::async_trait]
impl ConversationStore for PgConversationStore {
    /// Creates a new active conversation.
    async fn create_conversation(
        &self,
        tenant_id: &str,
        conversation: &NewConversation<'_>,
    ) -> Result<Conversation> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let row = sqlx::query_as::<_, ConversationRow>(
            r#"
            INSERT INTO conversations (
                tenant_id, conversation_id, owner_id, title
            ) VALUES ($1, $2, $3, $4)
            RETURNING conversation_id, owner_id, title, revision,
                      active_generation_id, created_at, updated_at, deleted_at
            "#,
        )
        .bind(tenant_id)
        .bind(conversation.conversation_id)
        .bind(conversation.owner_id)
        .bind(conversation.title)
        .fetch_one(&mut *transaction)
        .await
        .context("Failed to create conversation")?;
        transaction
            .commit()
            .await
            .context("Failed to commit conversation creation")?;
        Ok(Self::map_conversation(row))
    }

    /// Lists current conversations without deleted rows.
    async fn list_conversations(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<Conversation>> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query_as::<_, ConversationRow>(
            r#"
            SELECT conversation_id, owner_id, title, revision,
                   active_generation_id, created_at, updated_at, deleted_at
            FROM conversations
            WHERE tenant_id = $1 AND deleted_at IS NULL
            ORDER BY updated_at DESC, conversation_id
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit).context("Conversation limit overflow")?)
        .fetch_all(&mut *transaction)
        .await
        .context("Failed to list conversations")?;
        transaction
            .commit()
            .await
            .context("Failed to commit conversation list")?;
        Ok(rows.into_iter().map(Self::map_conversation).collect())
    }

    /// Loads one current conversation and its current messages.
    async fn get_conversation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        include_deleted_messages: bool,
    ) -> Result<Option<Conversation>> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let conversation = Self::load_conversation(
            &mut transaction,
            tenant_id,
            conversation_id,
            include_deleted_messages,
        )
        .await?;
        transaction
            .commit()
            .await
            .context("Failed to commit conversation read")?;
        Ok(conversation)
    }

    /// Changes a title only when the expected revision still matches.
    async fn update_conversation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        expected_revision: i64,
        title: &str,
    ) -> Result<Conversation> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let row = sqlx::query_as::<_, ConversationRow>(
            r#"
            UPDATE conversations
            SET title = $4, revision = revision + 1, updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND revision = $3 AND deleted_at IS NULL
              AND active_generation_id IS NULL
            RETURNING conversation_id, owner_id, title, revision,
                      active_generation_id, created_at, updated_at, deleted_at
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(expected_revision)
        .bind(title)
        .fetch_optional(&mut *transaction)
        .await
        .context("Failed to update conversation")?
        .context("Conversation revision changed or generation is active")?;
        transaction
            .commit()
            .await
            .context("Failed to commit conversation update")?;
        Ok(Self::map_conversation(row))
    }

    /// Soft-deletes a conversation without erasing message history.
    async fn delete_conversation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        expected_revision: i64,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let result = sqlx::query(
            r#"
            UPDATE conversations
            SET deleted_at = NOW(), revision = revision + 1, updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND revision = $3 AND deleted_at IS NULL
              AND active_generation_id IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(expected_revision)
        .execute(&mut *transaction)
        .await
        .context("Failed to delete conversation")?;
        if result.rows_affected() != 1 {
            bail!("Conversation revision changed or generation is active");
        }
        transaction
            .commit()
            .await
            .context("Failed to commit conversation deletion")
    }

    /// Inserts a current message and its attachment references atomically.
    async fn create_message(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<ConversationMessage> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let available: bool = sqlx::query_scalar(
            r#"
            SELECT EXISTS(
                SELECT 1 FROM conversations
                WHERE tenant_id = $1 AND conversation_id = $2
                  AND deleted_at IS NULL AND active_generation_id IS NULL
                FOR UPDATE
            )
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .fetch_one(&mut *transaction)
        .await
        .context("Failed to lock conversation")?;
        if !available {
            bail!("Conversation is unavailable or generation is active");
        }
        Self::insert_message(
            &mut transaction,
            tenant_id,
            conversation_id,
            message,
        )
        .await?;
        sqlx::query(
            r#"
            UPDATE conversations
            SET revision = revision + 1, updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .execute(&mut *transaction)
        .await?;
        let conversation = Self::load_conversation(
            &mut transaction,
            tenant_id,
            conversation_id,
            false,
        )
        .await?
        .context("Conversation disappeared during message creation")?;
        let created = conversation
            .messages
            .into_iter()
            .find(|candidate| candidate.message_id == message.message_id)
            .context("Created message is unavailable")?;
        transaction
            .commit()
            .await
            .context("Failed to commit message creation")?;
        Ok(created)
    }

    /// Replaces editable user content while preserving the previous revision.
    async fn update_message(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        message_id: &str,
        expected_revision: i64,
        content: &str,
        attachments: &[MessageAttachment],
        changed_by: &str,
    ) -> Result<ConversationMessage> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let updated = sqlx::query(
            r#"
            UPDATE conversation_messages AS message
            SET content = $5, revision = message.revision + 1,
                updated_at = NOW()
            FROM conversations AS conversation
            WHERE message.tenant_id = $1
              AND message.conversation_id = $2
              AND message.message_id = $3
              AND message.revision = $4
              AND message.role = 'user'
              AND message.created_by = $6
              AND message.deleted_at IS NULL
              AND conversation.tenant_id = message.tenant_id
              AND conversation.conversation_id = message.conversation_id
              AND conversation.deleted_at IS NULL
              AND conversation.active_generation_id IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(message_id)
        .bind(expected_revision)
        .bind(content)
        .bind(changed_by)
        .execute(&mut *transaction)
        .await
        .context("Failed to update message")?;
        if updated.rows_affected() != 1 {
            bail!(
                "Message revision changed, is not editable, or generation is active"
            );
        }
        sqlx::query(
            r#"
            DELETE FROM conversation_message_attachments
            WHERE tenant_id = $1 AND conversation_id = $2 AND message_id = $3
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(message_id)
        .execute(&mut *transaction)
        .await?;
        Self::insert_attachments(
            &mut transaction,
            tenant_id,
            conversation_id,
            message_id,
            attachments,
        )
        .await?;
        Self::append_message_revision(
            &mut transaction,
            tenant_id,
            conversation_id,
            message_id,
            changed_by,
        )
        .await?;
        sqlx::query(
            r#"
            UPDATE conversations
            SET revision = revision + 1, updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .execute(&mut *transaction)
        .await?;
        let conversation = Self::load_conversation(
            &mut transaction,
            tenant_id,
            conversation_id,
            false,
        )
        .await?
        .context("Conversation disappeared during message update")?;
        let result = conversation
            .messages
            .into_iter()
            .find(|candidate| candidate.message_id == message_id)
            .context("Updated message is unavailable")?;
        transaction.commit().await?;
        Ok(result)
    }

    /// Soft-deletes an editable message while preserving its prior revision.
    async fn delete_message(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        message_id: &str,
        expected_revision: i64,
        changed_by: &str,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let deleted = sqlx::query(
            r#"
            UPDATE conversation_messages AS message
            SET deleted_at = NOW(), revision = message.revision + 1,
                updated_at = NOW()
            FROM conversations AS conversation
            WHERE message.tenant_id = $1
              AND message.conversation_id = $2
              AND message.message_id = $3
              AND message.revision = $4
              AND message.role = 'user'
              AND message.created_by = $5
              AND message.deleted_at IS NULL
              AND conversation.tenant_id = message.tenant_id
              AND conversation.conversation_id = message.conversation_id
              AND conversation.deleted_at IS NULL
              AND conversation.active_generation_id IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(message_id)
        .bind(expected_revision)
        .bind(changed_by)
        .execute(&mut *transaction)
        .await
        .context("Failed to delete message")?;
        if deleted.rows_affected() != 1 {
            bail!(
                "Message revision changed, is not editable, or generation is active"
            );
        }
        Self::append_message_revision(
            &mut transaction,
            tenant_id,
            conversation_id,
            message_id,
            changed_by,
        )
        .await?;
        sqlx::query(
            r#"
            UPDATE conversations
            SET revision = revision + 1, updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .execute(&mut *transaction)
        .await?;
        transaction
            .commit()
            .await
            .context("Failed to commit message deletion")
    }

    /// Reserves one conversation generation and inserts its user message.
    async fn begin_generation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        generation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<Conversation> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let reserved = sqlx::query(
            r#"
            UPDATE conversations
            SET active_generation_id = $3, revision = revision + 1,
                updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND active_generation_id IS NULL AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(generation_id)
        .execute(&mut *transaction)
        .await
        .context("Failed to reserve conversation generation")?;
        if reserved.rows_affected() != 1 {
            bail!("Conversation is unavailable or already generating");
        }
        Self::insert_message(
            &mut transaction,
            tenant_id,
            conversation_id,
            message,
        )
        .await?;
        let conversation = Self::load_conversation(
            &mut transaction,
            tenant_id,
            conversation_id,
            false,
        )
        .await?
        .context("Reserved conversation is unavailable")?;
        transaction
            .commit()
            .await
            .context("Failed to commit generation reservation")?;
        Ok(conversation)
    }

    /// Persists an assistant answer and releases the generation reservation.
    async fn complete_generation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        generation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        Self::insert_message(
            &mut transaction,
            tenant_id,
            conversation_id,
            message,
        )
        .await?;
        let completed_user = sqlx::query(
            r#"
            UPDATE conversation_messages
            SET status = 'completed', revision = revision + 1,
                updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND message_id = $3 AND status = 'pending'
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(generation_id)
        .execute(&mut *transaction)
        .await?;
        if completed_user.rows_affected() != 1 {
            bail!("Pending user message is unavailable");
        }
        Self::append_message_revision(
            &mut transaction,
            tenant_id,
            conversation_id,
            generation_id,
            message.created_by,
        )
        .await?;
        let released = sqlx::query(
            r#"
            UPDATE conversations
            SET active_generation_id = NULL, revision = revision + 1,
                updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND active_generation_id = $3 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(generation_id)
        .execute(&mut *transaction)
        .await?;
        if released.rows_affected() != 1 {
            bail!("Generation reservation changed before completion");
        }
        transaction
            .commit()
            .await
            .context("Failed to commit generation completion")
    }

    /// Marks the user request failed and releases its generation reservation.
    async fn fail_generation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        generation_id: &str,
        message_id: &str,
        changed_by: &str,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant_id).await?;
        let failed = sqlx::query(
            r#"
            UPDATE conversation_messages
            SET status = 'failed', revision = revision + 1,
                updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND message_id = $3 AND status = 'pending'
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(message_id)
        .execute(&mut *transaction)
        .await?;
        if failed.rows_affected() != 1 {
            bail!("Pending user message is unavailable");
        }
        Self::append_message_revision(
            &mut transaction,
            tenant_id,
            conversation_id,
            message_id,
            changed_by,
        )
        .await?;
        let released = sqlx::query(
            r#"
            UPDATE conversations
            SET active_generation_id = NULL, revision = revision + 1,
                updated_at = NOW()
            WHERE tenant_id = $1 AND conversation_id = $2
              AND active_generation_id = $3
            "#,
        )
        .bind(tenant_id)
        .bind(conversation_id)
        .bind(generation_id)
        .execute(&mut *transaction)
        .await?;
        if released.rows_affected() != 1 {
            bail!("Generation reservation changed before failure persistence");
        }
        transaction
            .commit()
            .await
            .context("Failed to commit generation failure")
    }
}

#[cfg(test)]
mod tests {
    use anyhow::{Context, Result};
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;

    use super::*;

    const ROLE_SQL: &str = r#"
        CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS
            PASSWORD 'galadril_app';
        GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;
    "#;
    /// Starts a real RLS-enforced store for persistence contract tests.
    async fn store() -> Result<(
        testcontainers_modules::testcontainers::ContainerAsync<Postgres>,
        PgConversationStore,
    )> {
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
        Ok((container, PgConversationStore::new(database)))
    }

    #[test]
    fn timestamp_conversion_preserves_milliseconds() {
        let timestamp =
            OffsetDateTime::from_unix_timestamp_nanos(1_234_000_000);
        assert!(
            matches!(timestamp, Ok(value) if PgConversationStore::to_ms(value) == 1_234)
        );
    }

    #[tokio::test]
    async fn message_edits_deletions_and_failed_generations_are_versioned()
    -> Result<()> {
        let (_container, store) = store().await?;
        let tenant_id = "tenant_a";
        let conversation_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        store
            .create_conversation(
                tenant_id,
                &NewConversation {
                    conversation_id,
                    owner_id: "user_a",
                    title: "Persistent chat",
                },
            )
            .await?;

        let attachment = MessageAttachment {
            object_key: "tenant_a/images/one.png".to_owned(),
            kind: crate::application::ports::conversation_store::AttachmentKind::Image,
            file_name: Some("one.png".to_owned()),
            content_type: Some("image/png".to_owned()),
            size_bytes: Some(42),
        };
        let created = store
            .create_message(
                tenant_id,
                conversation_id,
                &NewConversationMessage {
                    message_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    role: MessageRole::User,
                    content: "first",
                    model_alias: None,
                    status: MessageStatus::Completed,
                    created_by: "user_a",
                    attachments: std::slice::from_ref(&attachment),
                },
            )
            .await?;
        anyhow::ensure!(created.revision == 1);
        anyhow::ensure!(created.attachments == [attachment]);

        let edited = store
            .update_message(
                tenant_id,
                conversation_id,
                &created.message_id,
                created.revision,
                "edited",
                &[],
                "user_a",
            )
            .await?;
        anyhow::ensure!(edited.revision == 2);
        anyhow::ensure!(edited.content == "edited");
        anyhow::ensure!(edited.attachments.is_empty());
        store
            .delete_message(
                tenant_id,
                conversation_id,
                &edited.message_id,
                edited.revision,
                "user_a",
            )
            .await?;
        let with_deleted = store
            .get_conversation(tenant_id, conversation_id, true)
            .await?
            .context("Conversation is missing")?;
        let deleted = with_deleted
            .messages
            .iter()
            .find(|message| message.message_id == edited.message_id)
            .context("Deleted message is missing")?;
        anyhow::ensure!(deleted.revision == 3);
        anyhow::ensure!(deleted.deleted_at_ms.is_some());

        let generation_id = "cccccccccccccccccccccccccccccccc";
        store
            .begin_generation(
                tenant_id,
                conversation_id,
                generation_id,
                &NewConversationMessage {
                    message_id: generation_id,
                    role: MessageRole::User,
                    content: "fail safely",
                    model_alias: Some("writer"),
                    status: MessageStatus::Pending,
                    created_by: "user_a",
                    attachments: &[],
                },
            )
            .await?;
        store
            .fail_generation(
                tenant_id,
                conversation_id,
                generation_id,
                generation_id,
                "user_a",
            )
            .await?;
        let failed = store
            .get_conversation(tenant_id, conversation_id, false)
            .await?
            .context("Conversation is missing")?;
        let failed_message = failed
            .messages
            .iter()
            .find(|message| message.message_id == generation_id)
            .context("Failed message is missing")?;
        anyhow::ensure!(failed_message.status == MessageStatus::Failed);
        anyhow::ensure!(failed_message.revision == 2);
        anyhow::ensure!(failed.active_generation_id.is_none());
        Ok(())
    }
}
