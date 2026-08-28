//! Persistence contracts for durable tenant conversations and messages.

use anyhow::Result;

/// Supported media kinds accepted by Scribe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttachmentKind {
    Image,
    Audio,
}

impl AttachmentKind {
    /// Returns the canonical persistence and GraphQL value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Image => "image",
            Self::Audio => "audio",
        }
    }

    /// Parses a canonical database value without silently widening support.
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "image" => Ok(Self::Image),
            "audio" => Ok(Self::Audio),
            _ => anyhow::bail!("Unsupported attachment kind: {value}"),
        }
    }
}

/// Durable metadata for one S3-backed message attachment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MessageAttachment {
    pub object_key: String,
    pub kind: AttachmentKind,
    pub file_name: Option<String>,
    pub content_type: Option<String>,
    pub size_bytes: Option<i64>,
}

/// Stable participant roles persisted for conversational history.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageRole {
    User,
    Assistant,
    System,
}

impl MessageRole {
    /// Returns the canonical persistence and GraphQL value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Assistant => "assistant",
            Self::System => "system",
        }
    }

    /// Parses a trusted database role while rejecting schema drift.
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "user" => Ok(Self::User),
            "assistant" => Ok(Self::Assistant),
            "system" => Ok(Self::System),
            _ => anyhow::bail!("Unsupported message role: {value}"),
        }
    }
}

/// Terminal and in-flight message states.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageStatus {
    Pending,
    Completed,
    Failed,
}

impl MessageStatus {
    /// Returns the canonical database value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Completed => "completed",
            Self::Failed => "failed",
        }
    }

    /// Parses a trusted database status while rejecting schema drift.
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "pending" => Ok(Self::Pending),
            "completed" => Ok(Self::Completed),
            "failed" => Ok(Self::Failed),
            _ => anyhow::bail!("Unsupported message status: {value}"),
        }
    }
}

/// One current message projection with immutable history stored separately.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConversationMessage {
    pub message_id: String,
    pub role: MessageRole,
    pub content: String,
    pub model_alias: Option<String>,
    pub status: MessageStatus,
    pub revision: i64,
    pub created_by: String,
    pub attachments: Vec<MessageAttachment>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub deleted_at_ms: Option<i64>,
}

/// Tenant conversation projection returned to authorized callers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Conversation {
    pub conversation_id: String,
    pub owner_id: String,
    pub title: String,
    pub revision: i64,
    pub active_generation_id: Option<String>,
    pub messages: Vec<ConversationMessage>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub deleted_at_ms: Option<i64>,
}

/// Input used to create a conversation.
pub struct NewConversation<'a> {
    pub conversation_id: &'a str,
    pub owner_id: &'a str,
    pub title: &'a str,
}

/// Input used to create a durable message.
pub struct NewConversationMessage<'a> {
    pub message_id: &'a str,
    pub role: MessageRole,
    pub content: &'a str,
    pub model_alias: Option<&'a str>,
    pub status: MessageStatus,
    pub created_by: &'a str,
    pub attachments: &'a [MessageAttachment],
}

/// PostgreSQL-facing operations that preserve optimistic revisions.
#[async_trait::async_trait]
pub trait ConversationStore: Send + Sync {
    /// Creates a new active conversation.
    async fn create_conversation(
        &self,
        tenant_id: &str,
        conversation: &NewConversation<'_>,
    ) -> Result<Conversation>;

    /// Lists current conversations without deleted rows.
    async fn list_conversations(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<Conversation>>;

    /// Loads one current conversation and its current messages.
    async fn get_conversation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        include_deleted_messages: bool,
    ) -> Result<Option<Conversation>>;

    /// Changes a title only when the expected revision still matches.
    async fn update_conversation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        expected_revision: i64,
        title: &str,
    ) -> Result<Conversation>;

    /// Soft-deletes a conversation without erasing message history.
    async fn delete_conversation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        expected_revision: i64,
    ) -> Result<()>;

    /// Inserts a current message and its attachment references atomically.
    async fn create_message(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<ConversationMessage>;

    /// Replaces editable user content while preserving the previous revision.
    #[expect(
        clippy::too_many_arguments,
        reason = "optimistic message edits require explicit revision provenance"
    )]
    async fn update_message(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        message_id: &str,
        expected_revision: i64,
        content: &str,
        attachments: &[MessageAttachment],
        changed_by: &str,
    ) -> Result<ConversationMessage>;

    /// Soft-deletes an editable message while preserving its prior revision.
    async fn delete_message(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        message_id: &str,
        expected_revision: i64,
        changed_by: &str,
    ) -> Result<()>;

    /// Reserves one conversation generation and inserts its user message.
    async fn begin_generation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        generation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<Conversation>;

    /// Persists an assistant answer and releases the generation reservation.
    async fn complete_generation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        generation_id: &str,
        message: &NewConversationMessage<'_>,
    ) -> Result<()>;

    /// Marks the user request failed and releases its generation reservation.
    async fn fail_generation(
        &self,
        tenant_id: &str,
        conversation_id: &str,
        generation_id: &str,
        message_id: &str,
        changed_by: &str,
    ) -> Result<()>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persisted_enum_values_are_canonical_and_fail_closed() {
        assert_eq!(AttachmentKind::Image.as_str(), "image");
        assert_eq!(MessageRole::Assistant.as_str(), "assistant");
        assert_eq!(MessageStatus::Completed.as_str(), "completed");
        assert!(AttachmentKind::parse("document").is_err());
        assert!(MessageRole::parse("tool").is_err());
        assert!(MessageStatus::parse("unknown").is_err());
    }
}
