//! Request-scoped Scribe adapter with authorized, read-only tenant search.

use std::sync::Arc;

use anyhow::{Context, Result};
use futures::stream;
use scribe::engine::{
    AttachmentUrl, MessageRole as ScribeMessageRole, ScribeConfig,
    ScribeEngine, ScribeRequest, ScribeStreamChunk, SerializableMessage,
    SerializableSession,
};
use scribe::{DatabaseProvider, ScribeCompletionStatus};
use serde_json::{Value, json};

use crate::application::ports::conversation_agent::{
    AgentChunk, AgentRequest, AgentStream, ConversationAgent,
};
use crate::application::ports::conversation_store::{
    AttachmentKind, MessageRole,
};
use crate::application::usecases::audit::{
    AuditAction, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::QueryContext;
use crate::application::usecases::search::{
    GlobalSearchHit, SearchService, StructuredSearchQuery,
};

struct TenantSearchProvider {
    search: Arc<SearchService>,
    audit: Arc<AuditService>,
    tenant_id: String,
    user_id: String,
    conversation_id: String,
    context: QueryContext,
}

impl TenantSearchProvider {
    /// Converts authorized search hits into compact JSON evidence.
    fn evidence_value(hit: GlobalSearchHit) -> Value {
        match hit {
            GlobalSearchHit::EntityState { entity_id, state } => json!({
                "kind": "entity_state",
                "entity_id": entity_id,
                "state": state,
            }),
            GlobalSearchHit::Event {
                event_id,
                event_type,
                event_time_ms,
                properties,
            } => json!({
                "kind": "event",
                "event_id": event_id,
                "event_type": event_type,
                "event_time_ms": event_time_ms,
                "properties": properties,
            }),
            GlobalSearchHit::Embedding {
                entity_id,
                modality,
                created_at_ms,
                metadata,
                score,
            } => json!({
                "kind": "embedding",
                "entity_id": entity_id,
                "modality": modality,
                "created_at_ms": created_at_ms,
                "metadata": metadata,
                "score": score,
            }),
        }
    }
}

#[async_trait::async_trait]
impl DatabaseProvider for TenantSearchProvider {
    /// Executes only Gateway's permission-filtered, RLS-scoped search path.
    async fn query_database(
        &self,
        query: &scribe::tools::database::DatabaseQuery,
    ) -> Result<Option<String>> {
        let operation = self
            .audit
            .begin(
                &self.tenant_id,
                &self.user_id,
                AuditTarget::new(
                    AuditAction::ScribeDatabaseQuery,
                    "conversation",
                    &self.conversation_id,
                )
                .with_details(json!({
                    "max_results": query.max_results,
                    "has_scope": query.scope.is_some(),
                    "keyword_count": query.keywords.len(),
                })),
                &self.context,
            )
            .await?;
        let results = self
            .search
            .structured_search(
                &self.tenant_id,
                &self.user_id,
                &self.context,
                StructuredSearchQuery {
                    text: Some(&query.question),
                    ..StructuredSearchQuery::default()
                },
                usize::from(query.max_results),
            )
            .await;
        match results {
            Ok(results) => {
                let evidence = if results.is_empty() {
                    None
                } else {
                    let values = results
                        .into_iter()
                        .map(Self::evidence_value)
                        .collect::<Vec<_>>();
                    Some(
                        serde_json::to_string(&values)
                            .context("Failed to serialize Scribe evidence")?,
                    )
                };
                operation.succeeded().await?;
                Ok(evidence)
            },
            Err(error) => {
                operation.failed("authorized_search_failed").await?;
                Err(error)
            },
        }
    }
}

/// Direct library adapter for multi-model Scribe generation.
pub struct ScribeAgent {
    engine: Arc<ScribeEngine>,
    search: Arc<SearchService>,
    audit: Arc<AuditService>,
    system_prompt: String,
}

impl ScribeAgent {
    /// Loads configured Scribe models and starts the completion observer.
    pub async fn new(
        config: ScribeConfig,
        search: Arc<SearchService>,
        audit: Arc<AuditService>,
    ) -> Result<Arc<Self>> {
        let system_prompt = config.system_prompt.clone();
        let (completion_tx, mut completion_rx) =
            tokio::sync::mpsc::channel(256);
        let engine = ScribeEngine::new(config, completion_tx).await?;
        std::mem::drop(tokio::spawn(async move {
            while let Some(event) = completion_rx.recv().await {
                tracing::info!(
                    event.name = "gateway.scribe.terminal_observed",
                    session.id = event.session.session_id,
                    message.id = event.message_id,
                    status = match event.status {
                        ScribeCompletionStatus::Completed => "completed",
                        ScribeCompletionStatus::Failed => "failed",
                    },
                    "Scribe emitted a terminal persistence signal"
                );
            }
        }));
        Ok(Arc::new(Self {
            engine,
            search,
            audit,
            system_prompt,
        }))
    }

    /// Converts Gateway attachment URLs into Scribe's explicit media enum.
    fn attachments(
        attachments: impl IntoIterator<Item = crate::application::ports::conversation_agent::AgentAttachment>,
    ) -> Vec<AttachmentUrl> {
        attachments
            .into_iter()
            .map(|attachment| match attachment.kind {
                AttachmentKind::Image => AttachmentUrl::Image(attachment.url),
                AttachmentKind::Audio => AttachmentUrl::Audio(attachment.url),
            })
            .collect()
    }

    /// Maps Gateway roles into Scribe's stable persisted role vocabulary.
    const fn role(role: MessageRole) -> ScribeMessageRole {
        match role {
            MessageRole::User => ScribeMessageRole::User,
            MessageRole::Assistant => ScribeMessageRole::Assistant,
            MessageRole::System => ScribeMessageRole::System,
        }
    }
}

#[async_trait::async_trait]
impl ConversationAgent for ScribeAgent {
    /// Hydrates durable history and starts one tenant-bound Scribe request.
    async fn start(&self, request: AgentRequest<'_>) -> Result<AgentStream> {
        let session_id =
            format!("{}/{}", request.tenant_id, request.conversation_id);
        let mut history = Vec::with_capacity(request.history.len() + 1);
        if !self.system_prompt.is_empty() {
            history.push(SerializableMessage {
                message_id: None,
                model_alias: None,
                role: ScribeMessageRole::System,
                content: self.system_prompt.clone(),
                attachments: Vec::new(),
            });
        }
        for message in request.history {
            history.push(SerializableMessage {
                message_id: Some(message.message_id),
                model_alias: message.model_alias,
                role: Self::role(message.role),
                content: message.content,
                attachments: Self::attachments(message.attachments),
            });
        }
        let revision = u64::try_from(request.session_revision)
            .context("Conversation revision is negative")?;
        self.engine
            .hydrate_conversation_from_db(&SerializableSession {
                session_id: session_id.clone(),
                revision,
                messages: history,
            })
            .await?;
        let provider = Arc::new(TenantSearchProvider {
            search: Arc::clone(&self.search),
            audit: Arc::clone(&self.audit),
            tenant_id: request.tenant_id.to_owned(),
            user_id: request.user_id.to_owned(),
            conversation_id: request.conversation_id.to_owned(),
            context: request.authorization.clone(),
        });
        let stream = self
            .engine
            .execute_request(ScribeRequest {
                session_id,
                message_id: request.message_id.to_owned(),
                model_alias: request.model_alias.map(str::to_owned),
                prompt: request.prompt.to_owned(),
                attachments: Self::attachments(request.attachments),
                grammar_constraint: None,
                database_provider: Some(provider),
            })
            .await?;
        Ok(Box::pin(stream::unfold(stream, |mut stream| async move {
            stream.recv().await.map(|result| {
                let mapped = result.map(|chunk| match chunk {
                    ScribeStreamChunk::Content(content) => {
                        AgentChunk::Content(content)
                    },
                    ScribeStreamChunk::Reasoning(reasoning) => {
                        AgentChunk::Reasoning(reasoning)
                    },
                });
                (mapped, stream)
            })
        })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_evidence_preserves_domain_identity() {
        let value = TenantSearchProvider::evidence_value(
            GlobalSearchHit::EntityState {
                entity_id: "entity-1".to_owned(),
                state: json!({"status": "active"}),
            },
        );
        assert_eq!(value.get("kind"), Some(&json!("entity_state")));
        assert_eq!(value.get("entity_id"), Some(&json!("entity-1")));
    }

    #[test]
    fn roles_and_attachments_map_without_implicit_media_types() {
        assert_eq!(
            ScribeAgent::role(MessageRole::Assistant),
            ScribeMessageRole::Assistant
        );
        let attachments = ScribeAgent::attachments([
            crate::application::ports::conversation_agent::AgentAttachment {
                kind: AttachmentKind::Audio,
                url: "https://s3/audio".to_owned(),
            },
        ]);
        assert!(matches!(attachments.first(), Some(AttachmentUrl::Audio(_))));
    }
}
