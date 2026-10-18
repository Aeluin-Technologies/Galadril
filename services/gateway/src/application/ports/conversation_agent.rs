//! Port for request-scoped conversational generation.

use std::pin::Pin;

use anyhow::Result;
use futures::Stream;
use serde::Deserialize;

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
    pub message_id: &'a str,
    pub model_alias: Option<&'a str>,
    pub reasoning_effort: Option<&'a str>,
    pub prompt: &'a str,
    pub history: Vec<AgentHistoryMessage>,
    pub attachments: Vec<AgentAttachment>,
    pub authorization: &'a QueryContext,
}

/// Stream elements exposed to GraphQL without persisting hidden reasoning.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentChunk {
    Content(String),
}

/// Owned asynchronous output from a conversational agent.
pub type AgentStream =
    Pin<Box<dyn Stream<Item = Result<AgentChunk>> + Send + 'static>>;

/// Public model metadata excludes provider endpoints and credentials.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentModel {
    pub alias: String,
    pub provider: String,
    pub model: String,
    pub reasoning_efforts: Vec<String>,
    pub default_reasoning_effort: Option<String>,
}

/// An empty catalog represents an intentionally disabled inference runtime.
#[derive(Debug, Default, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentModelCatalog {
    pub default_model: Option<String>,
    pub models: Vec<AgentModel>,
}

/// Generates responses while preserving the request's tenant security scope.
#[async_trait::async_trait]
pub trait ConversationAgent: Send + Sync {
    async fn document_operation(
        &self,
        _operation: DocumentOperation,
        _request: serde_json::Value,
    ) -> Result<serde_json::Value> {
        anyhow::bail!("Document inference is unavailable")
    }
    /// Disabled runtimes expose no installed tools and acquire no authority.
    async fn capabilities(
        &self,
    ) -> Result<crate::domain::capabilities::CapabilityCatalog> {
        Ok(crate::domain::capabilities::CapabilityCatalog {
            schema_version: 1,
            capabilities: Vec::new(),
        })
    }

    /// Detached Pengolo jobs remain owned by Scribe after this request
    /// returns.
    async fn submit_generation(
        &self,
        _request: serde_json::Value,
    ) -> Result<()> {
        anyhow::bail!("Background generation is unavailable")
    }

    /// Human responses remain bound to the generation's private lease.
    async fn answer_generation(
        &self,
        _id: &str,
        _capability: &str,
        _answer: serde_json::Value,
    ) -> Result<()> {
        anyhow::bail!("Human interaction is unavailable")
    }

    async fn cancel_generation(
        &self,
        _id: &str,
        _capability: &str,
    ) -> Result<()> {
        anyhow::bail!("Background generation is unavailable")
    }
    /// Lists administrator-approved models without triggering inference.
    async fn models(&self) -> Result<AgentModelCatalog>;

    /// Starts one generation and returns a stream that remains owned by
    /// Gateway.
    async fn start(&self, request: AgentRequest<'_>) -> Result<AgentStream>;
}

#[derive(Clone, Copy)]
pub enum DocumentOperation {
    Parse,
    Embed,
    Extract,
    Rank,
    Recognize,
}

impl DocumentOperation {
    pub const fn path(self) -> &'static str {
        match self {
            Self::Parse => "parse",
            Self::Embed => "embed",
            Self::Extract => "extract",
            Self::Rank => "rank",
            Self::Recognize => "recognize",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_chunks_preserve_content() {
        assert_ne!(
            AgentChunk::Content("same".to_owned()),
            AgentChunk::Content("different".to_owned())
        );
    }
}
