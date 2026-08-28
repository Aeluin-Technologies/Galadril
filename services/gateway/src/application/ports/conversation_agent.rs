//! Port for request-scoped conversational generation.

use std::pin::Pin;

use anyhow::Result;
use futures::Stream;

use crate::application::ports::conversation_store::{
    AttachmentKind, MessageRole,
};
use crate::application::usecases::authorization::QueryContext;

/// Runtime attachment containing a short-lived, tenant-validated URL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentAttachment {
    pub kind: AttachmentKind,
    pub url: String,
}

/// Persisted message converted to model-safe runtime attachment URLs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentHistoryMessage {
    pub message_id: String,
    pub role: MessageRole,
    pub content: String,
    pub model_alias: Option<String>,
    pub attachments: Vec<AgentAttachment>,
}

/// Immutable request context passed into one Scribe generation.
pub struct AgentRequest<'a> {
    pub tenant_id: &'a str,
    pub user_id: &'a str,
    pub conversation_id: &'a str,
    pub session_revision: i64,
    pub message_id: &'a str,
    pub model_alias: Option<&'a str>,
    pub prompt: &'a str,
    pub history: Vec<AgentHistoryMessage>,
    pub attachments: Vec<AgentAttachment>,
    pub authorization: &'a QueryContext,
}

/// Stream elements exposed to GraphQL without persisting hidden reasoning.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentChunk {
    Content(String),
    Reasoning(String),
}

/// Owned asynchronous output from a conversational agent.
pub type AgentStream =
    Pin<Box<dyn Stream<Item = Result<AgentChunk>> + Send + 'static>>;

/// Generates responses while preserving the request's tenant security scope.
#[async_trait::async_trait]
pub trait ConversationAgent: Send + Sync {
    /// Starts one generation and returns a stream that remains owned by
    /// Gateway.
    async fn start(&self, request: AgentRequest<'_>) -> Result<AgentStream>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_chunks_keep_reasoning_distinct_from_user_content() {
        assert_ne!(
            AgentChunk::Content("same".to_owned()),
            AgentChunk::Reasoning("same".to_owned())
        );
    }
}
