//! Conversation persistence, completion events, and attachments.

use anyhow::{Context as _, Result, anyhow};
use futures::future::join_all;
use mistralrs::{LlguidanceGrammar, MultimodalMessages, TextMessageRole};
use serde::{Deserialize, Serialize};

/// Enumerates explicitly supported message participants.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MessageRole {
    System,
    User,
    Assistant,
}

impl From<MessageRole> for TextMessageRole {
    fn from(role: MessageRole) -> Self {
        match role {
            MessageRole::System => TextMessageRole::System,
            MessageRole::User => TextMessageRole::User,
            MessageRole::Assistant => TextMessageRole::Assistant,
        }
    }
}

/// Rich media attachments for GraphRAG and multi-turn interaction.
#[derive(Debug, Clone)]
pub enum Attachment {
    Image(image::DynamicImage),
    Audio(Vec<u8>),
}

/// Remote S3 resource pointers for media persistence.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", content = "url")]
pub enum AttachmentUrl {
    Image(String),
    Audio(String),
}

/// Helper component to stream and transform S3 assets into memory frames.
pub struct S3AttachmentResolver;

impl S3AttachmentResolver {
    /// Downloads and decodes a single image from a remote URL.
    pub async fn resolve_image(
        client: &reqwest::Client,
        url: &str,
    ) -> Result<image::DynamicImage> {
        let response = client.get(url).send().await.with_context(|| {
            format!("Failed to dispatch request to S3 target: {}", url)
        })?;

        if !response.status().is_success() {
            return Err(anyhow!(
                "S3 returned status code error: {}",
                response.status()
            ));
        }

        let raw_bytes = response.bytes().await.with_context(
            || "Error reading full bytes from S3 network stream",
        )?;

        image::load_from_memory(&raw_bytes)
            .with_context(|| format!("Failed decoding image structural bytes into a matrix from: {}", url))
    }

    /// Concurrently resolves a slice of attachment URLs into memory assets.
    pub async fn resolve_all(
        client: &reqwest::Client,
        targets: &[AttachmentUrl],
    ) -> Result<Vec<Attachment>> {
        let futures = targets.iter().map(|target| async move {
            match target {
                AttachmentUrl::Image(url) => Self::resolve_image(client, url)
                    .await
                    .map(Attachment::Image),
                AttachmentUrl::Audio(url) => {
                    let response =
                        client.get(url).send().await.with_context(|| {
                            format!(
                                "failed to retrieve audio attachment: {url}"
                            )
                        })?;
                    if !response.status().is_success() {
                        return Err(anyhow!(
                            "attachment endpoint returned {} for {url}",
                            response.status()
                        ));
                    }
                    let bytes = response.bytes().await.with_context(|| {
                        format!("failed to read audio attachment: {url}")
                    })?;
                    if bytes.is_empty() {
                        return Err(anyhow!(
                            "audio attachment is empty: {url}"
                        ));
                    }
                    Ok(Attachment::Audio(bytes.to_vec()))
                },
            }
        });

        join_all(futures).await.into_iter().collect()
    }
}

/// A serializable entity representation optimized for zero-trust document and
/// SQL/NoSQL databases.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SerializableMessage {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_alias: Option<String>,
    pub role: MessageRole,
    pub content: String,
    pub attachments: Vec<AttachmentUrl>,
}

/// Database-ready serializable wrapper matching the target schema.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SerializableSession {
    pub session_id: String,
    #[serde(default)]
    pub revision: u64,
    pub messages: Vec<SerializableMessage>,
}

/// Terminal state emitted once for every accepted generation request.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScribeCompletionStatus {
    Completed,
    Failed,
}

/// Persistence-ready notification emitted after a generation terminates.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScribeCompletionEvent {
    pub message_id: String,
    pub model_alias: String,
    pub status: ScribeCompletionStatus,
    pub session: SerializableSession,
    pub final_content: Option<String>,
    pub error: Option<String>,
    pub runtime_attachments: Vec<AttachmentUrl>,
}

/// Backward-compatible name for the terminal persistence notification.
pub type ScribeOutputMessage = ScribeCompletionEvent;

/// Complete request envelope used for reliable, idempotent chat generation.
pub struct ScribeRequest {
    pub session_id: String,
    pub message_id: String,
    pub model_alias: Option<String>,
    pub prompt: String,
    pub attachments: Vec<AttachmentUrl>,
    pub grammar_constraint: Option<LlguidanceGrammar>,
}

/// Represents a stateful, thread-safe sequence of multi-modal messages.
#[derive(Clone)]
pub struct Conversation {
    pub history: MultimodalMessages,
    pub serializable_history: Vec<SerializableMessage>,
    pub(crate) revision: u64,
}

pub(crate) struct CompletedTurn<'a> {
    pub(crate) message_id: &'a str,
    pub(crate) model_alias: &'a str,
    pub(crate) prompt: &'a str,
    pub(crate) attachments: Vec<AttachmentUrl>,
    pub(crate) response: &'a str,
}

pub(crate) struct CachedTurn {
    pub(crate) model_alias: String,
    pub(crate) prompt: String,
    attachments: Vec<AttachmentUrl>,
    pub(crate) response: String,
}

impl CachedTurn {
    pub(crate) fn matches_request(
        &self,
        model_alias: &str,
        prompt: &str,
        attachments: &[AttachmentUrl],
    ) -> bool {
        self.model_alias == model_alias &&
            self.prompt == prompt &&
            self.attachments == attachments
    }
}

impl Conversation {
    /// Creates a new conversation initialized with an optional system prompt.
    pub fn new(system_prompt: &str) -> Self {
        let mut history = MultimodalMessages::new();
        let mut serializable_history = Vec::new();

        if !system_prompt.is_empty() {
            history =
                history.add_message(TextMessageRole::System, system_prompt);
            serializable_history.push(SerializableMessage {
                message_id: None,
                model_alias: None,
                role: MessageRole::System,
                content: system_prompt.to_string(),
                attachments: Vec::new(),
            });
        }

        Self {
            history,
            serializable_history,
            revision: 0,
        }
    }

    /// Hydrates a full multi-modal conversation from a serialized database
    /// schema session.
    pub async fn from_serializable(
        session: &SerializableSession,
        http_client: &reqwest::Client,
    ) -> Result<Self> {
        let mut history = MultimodalMessages::new();

        for msg in &session.messages {
            let role = TextMessageRole::from(msg.role);

            if !msg.attachments.is_empty() {
                let resolved = S3AttachmentResolver::resolve_all(
                    http_client,
                    &msg.attachments,
                )
                .await?;

                let images: Vec<_> = resolved
                    .into_iter()
                    .filter_map(|att| match att {
                        Attachment::Image(img) => Some(img),
                        _ => None,
                    })
                    .collect();

                if !images.is_empty() {
                    history =
                        history.add_image_message(role, &msg.content, images);
                    continue;
                }
            }

            history = history.add_message(role, &msg.content);
        }

        Ok(Self {
            history,
            serializable_history: session.messages.clone(),
            revision: session.revision,
        })
    }

    #[inline]
    pub(crate) fn revision(&self) -> u64 {
        self.revision
    }

    pub(crate) fn snapshot(&self, session_id: &str) -> SerializableSession {
        SerializableSession {
            session_id: session_id.to_owned(),
            revision: self.revision,
            messages: self.serializable_history.clone(),
        }
    }

    pub(crate) fn cached_turn(&self, message_id: &str) -> Option<CachedTurn> {
        let user = self.serializable_history.iter().find(|message| {
            message.role == MessageRole::User &&
                message.message_id.as_deref() == Some(message_id)
        })?;
        let assistant = self.serializable_history.iter().find(|message| {
            message.role == MessageRole::Assistant &&
                message.message_id.as_deref() == Some(message_id)
        })?;
        Some(CachedTurn {
            model_alias: assistant
                .model_alias
                .clone()
                .or_else(|| user.model_alias.clone())?,
            prompt: user.content.clone(),
            attachments: user.attachments.clone(),
            response: assistant.content.clone(),
        })
    }

    pub(crate) fn commit_turn(
        &mut self,
        turn: &CompletedTurn<'_>,
    ) -> Result<()> {
        if self.serializable_history.iter().any(|message| {
            message.message_id.as_deref() == Some(turn.message_id)
        }) {
            return Err(anyhow!(
                "message ID already committed: {}",
                turn.message_id
            ));
        }
        let next_revision = self
            .revision
            .checked_add(1)
            .context("conversation revision overflow")?;

        self.serializable_history.push(SerializableMessage {
            message_id: Some(turn.message_id.to_owned()),
            model_alias: Some(turn.model_alias.to_owned()),
            role: MessageRole::User,
            content: turn.prompt.to_owned(),
            attachments: turn.attachments.clone(),
        });
        self.serializable_history.push(SerializableMessage {
            message_id: Some(turn.message_id.to_owned()),
            model_alias: Some(turn.model_alias.to_owned()),
            role: MessageRole::Assistant,
            content: turn.response.to_owned(),
            attachments: Vec::new(),
        });
        self.revision = next_revision;
        Ok(())
    }
}
