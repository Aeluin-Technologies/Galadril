//! Authorized conversation lifecycle and durable Scribe orchestration.

use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use futures::{Stream, StreamExt as _};
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::application::ports::attachment_store::AttachmentStore;
use crate::application::ports::conversation_agent::{
    AgentChunk, AgentHistoryMessage, AgentRequest, ConversationAgent,
};
use crate::application::ports::conversation_store::{
    Conversation, ConversationMessage, ConversationStore, EvidenceSource,
    GenerationEvent, MessageAttachment, MessageRole, MessageStatus,
    NewConversation, NewConversationMessage,
};
use crate::application::usecases::audit::{
    AuditAction, AuditOperation, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;

const MAX_ATTACHMENTS: usize = 8;
const MAX_CONTENT_BYTES: usize = 64 * 1024;
const MAX_TITLE_BYTES: usize = 256;
const ATTACHMENT_URL_TTL: Duration = Duration::from_secs(15 * 60);

/// GraphQL-facing stream that remains backed by a persistence worker.
pub type ConversationStream =
    Pin<Box<dyn Stream<Item = Result<AgentChunk>> + Send + 'static>>;

/// Stable identifiers and streamed output for one accepted generation.
pub struct ConversationGeneration {
    pub message_id: String,
    pub response_message_id: String,
    pub stream: ConversationStream,
}

/// Coordinates authorization, persistence, attachments, Scribe, and audit.
pub struct ConversationService {
    store: Arc<dyn ConversationStore>,
    agent: Arc<dyn ConversationAgent>,
    attachments: Arc<dyn AttachmentStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
}

impl ConversationService {
    /// Revalidates historical evidence rather than trusting an earlier answer.
    async fn sources_visible(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        sources: &[EvidenceSource],
    ) -> Result<bool> {
        for source in sources {
            let ctx = QueryContext {
                entity_id: source.entity_id.clone(),
                modality: source.modality.clone(),
                state_type: source.state_type.clone(),
                hour_utc: i64::from(chrono::Timelike::hour(
                    &chrono::Utc::now(),
                )),
                ..context.clone()
            };
            if !self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    if source.manage {
                        Permission::Manage
                    } else {
                        Permission::View
                    },
                    &source.resource_type,
                    &source.resource_id,
                    Some(&ctx),
                )
                .await?
            {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// Reads replay batches only while both conversation and evidence remain
    /// visible.
    pub async fn generation_events(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        generation_id: &str,
        after: i32,
    ) -> Result<Vec<GenerationEvent>> {
        self.require_permission(
            tenant_id,
            user_id,
            context,
            Permission::View,
            "conversation",
            conversation_id,
        )
        .await?;
        let conversation = self
            .store
            .get_conversation(tenant_id, conversation_id, false)
            .await?
            .context("Conversation unavailable")?;
        let generation = conversation
            .messages
            .iter()
            .find(|message| {
                message.message_id == generation_id &&
                    message.role == MessageRole::User &&
                    message.created_by == user_id
            })
            .context("Generation unavailable")?;
        let sources = generation
            .evidence
            .as_deref()
            .context("Generation provenance unavailable")?;
        if !self
            .sources_visible(tenant_id, user_id, context, sources)
            .await?
        {
            bail!("Generation evidence access denied");
        }
        self.store
            .generation_events(
                tenant_id,
                conversation_id,
                generation_id,
                after,
                64,
            )
            .await
    }

    /// Creates the application service from reusable domain ports.
    pub fn new(
        store: Arc<dyn ConversationStore>,
        agent: Arc<dyn ConversationAgent>,
        attachments: Arc<dyn AttachmentStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
    ) -> Self {
        Self {
            store,
            agent,
            attachments,
            identity,
            auth,
            audit,
        }
    }

    /// Starts an audited operation and applies identity, SpiceDB, then Cedar.
    #[expect(
        clippy::too_many_arguments,
        reason = "authorization requires explicit actor, target, and permission context"
    )]
    async fn begin_authorized(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        target: AuditTarget,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
    ) -> Result<AuditOperation> {
        let operation = self
            .audit
            .begin(tenant_id, user_id, target, context)
            .await
            .context("Failed to persist conversation audit attempt")?;
        if let Err(error) = self.identity.verify_user(tenant_id, user_id).await
        {
            operation.denied("identity_denied").await?;
            return Err(error);
        }
        match self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                permission,
                resource_type,
                resource_id,
                Some(context),
            )
            .await
        {
            Ok(true) => Ok(operation),
            Ok(false) => {
                operation.denied("authorization_denied").await?;
                bail!("Authorization denied");
            },
            Err(error) => {
                operation.failed("authorization_dependency_failed").await?;
                Err(error)
                    .context("Failed to authorize conversation operation")
            },
        }
    }

    /// Requires an active identity and one resource permission for read paths.
    async fn require_permission(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
    ) -> Result<()> {
        self.identity.verify_user(tenant_id, user_id).await?;
        if !self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                permission,
                resource_type,
                resource_id,
                Some(context),
            )
            .await?
        {
            bail!("Authorization denied");
        }
        Ok(())
    }

    /// Validates user-controlled content without retaining duplicate copies.
    fn validate_content(content: &str, attachments: usize) -> Result<&str> {
        let content = content.trim();
        if content.is_empty() && attachments == 0 {
            bail!("Message content or an attachment is required");
        }
        if content.len() > MAX_CONTENT_BYTES {
            bail!("Message content exceeds {MAX_CONTENT_BYTES} bytes");
        }
        if attachments > MAX_ATTACHMENTS {
            bail!("A message supports at most {MAX_ATTACHMENTS} attachments");
        }
        Ok(content)
    }

    /// Validates and normalizes a user-controlled conversation title.
    fn validate_title(title: &str) -> Result<&str> {
        let title = title.trim();
        if title.is_empty() || title.len() > MAX_TITLE_BYTES {
            bail!(
                "Conversation title must contain 1 to {MAX_TITLE_BYTES} bytes"
            );
        }
        Ok(title)
    }

    /// Resolves attachments only after both raw-resource and S3 checks pass.
    async fn resolve_attachments(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        attachments: &[MessageAttachment],
    ) -> Result<
        Vec<crate::application::ports::conversation_agent::AgentAttachment>,
    > {
        for attachment in attachments {
            if !self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "raw",
                    &attachment.object_key,
                    Some(context),
                )
                .await?
            {
                bail!("Authorization denied for message attachment");
            }
        }
        self.attachments
            .resolve_for_scribe(
                tenant_id,
                user_id,
                attachments,
                ATTACHMENT_URL_TTL,
            )
            .await
    }

    /// Converts completed durable history into request-scoped model inputs.
    async fn resolve_history(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        messages: &[ConversationMessage],
        pending_message_id: &str,
    ) -> Result<Vec<AgentHistoryMessage>> {
        let prompt_bytes = messages
            .iter()
            .find(|message| message.message_id == pending_message_id)
            .map_or(0, |message| message.content.len());
        let budget = (MAX_CONTENT_BYTES / 2)
            .min(MAX_CONTENT_BYTES.saturating_sub(prompt_bytes));
        let mut bytes = 0;
        let mut history = Vec::with_capacity(messages.len().min(64));
        for message in messages.iter().rev() {
            if message.message_id == pending_message_id ||
                message.status != MessageStatus::Completed ||
                message.deleted_at_ms.is_some() ||
                message.created_by != user_id ||
                message.role == MessageRole::System
            {
                continue;
            }
            if message.role == MessageRole::Assistant {
                let Some(sources) = message.evidence.as_deref() else {
                    continue;
                };
                if !self
                    .sources_visible(tenant_id, user_id, context, sources)
                    .await?
                {
                    continue;
                }
            }
            if history.len() >= 64 || bytes + message.content.len() > budget {
                break;
            }
            bytes += message.content.len();
            let attachments = self
                .resolve_attachments(
                    tenant_id,
                    user_id,
                    context,
                    &message.attachments,
                )
                .await?;
            history.push(AgentHistoryMessage {
                message_id: message.message_id.clone(),
                role: message.role,
                content: message.content.clone(),
                model_alias: message.model_alias.clone(),
                attachments,
            });
        }
        history.reverse();
        Ok(history)
    }

    /// Creates a conversation and establishes its canonical SpiceDB ownership.
    pub async fn create_conversation(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        title: &str,
    ) -> Result<Conversation> {
        let title = Self::validate_title(title)?;
        let conversation_id = Uuid::new_v4().simple().to_string();
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::CreateConversation,
                    "conversation",
                    &conversation_id,
                ),
                Permission::CreateConversation,
                "tenant",
                tenant_id,
            )
            .await?;
        let conversation = match self
            .store
            .create_conversation(
                tenant_id,
                &NewConversation {
                    conversation_id: &conversation_id,
                    owner_id: user_id,
                    title,
                },
            )
            .await
        {
            Ok(conversation) => conversation,
            Err(error) => {
                operation.failed("database_mutation_failed").await?;
                return Err(error);
            },
        };
        let resource_id = format!("{tenant_id}/{conversation_id}");
        for (relation, subject_type, subject_id) in
            [("parent", "tenant", tenant_id), ("owner", "user", user_id)]
        {
            if let Err(error) = self
                .auth
                .upsert_relationship(
                    "conversation",
                    &resource_id,
                    relation,
                    subject_type,
                    subject_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
        }
        operation.succeeded().await?;
        Ok(conversation)
    }

    /// Lists only conversations for which the principal retains view access.
    pub async fn conversations(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<Conversation>> {
        self.identity.verify_user(tenant_id, user_id).await?;
        let candidates = self
            .store
            .list_conversations(tenant_id, limit.clamp(1, 100))
            .await?;
        let mut visible = Vec::with_capacity(candidates.len());
        for conversation in candidates {
            if self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "conversation",
                    &conversation.conversation_id,
                    Some(context),
                )
                .await?
            {
                visible.push(conversation);
            }
        }
        Ok(visible)
    }

    /// Loads one permission-checked conversation with its current messages.
    pub async fn conversation(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        include_deleted_messages: bool,
    ) -> Result<Option<Conversation>> {
        self.require_permission(
            tenant_id,
            user_id,
            context,
            Permission::View,
            "conversation",
            conversation_id,
        )
        .await?;
        let conversation = self
            .store
            .get_conversation(
                tenant_id,
                conversation_id,
                include_deleted_messages,
            )
            .await?;
        let Some(mut conversation) = conversation else {
            return Ok(None);
        };
        let mut visible = Vec::with_capacity(conversation.messages.len());
        for message in conversation.messages {
            if message.created_by != user_id {
                continue;
            }
            if message.role == MessageRole::Assistant {
                let Some(sources) = message.evidence.as_deref() else {
                    continue;
                };
                if !self
                    .sources_visible(tenant_id, user_id, context, sources)
                    .await?
                {
                    continue;
                }
            }
            visible.push(message);
        }
        conversation.messages = visible;
        Ok(Some(conversation))
    }

    /// Updates a conversation title through optimistic concurrency control.
    pub async fn update_conversation(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        expected_revision: i64,
        title: &str,
    ) -> Result<Conversation> {
        let title = Self::validate_title(title)?;
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::UpdateConversation,
                    "conversation",
                    conversation_id,
                )
                .with_revision_id(expected_revision.to_string()),
                Permission::Edit,
                "conversation",
                conversation_id,
            )
            .await?;
        match self
            .store
            .update_conversation(
                tenant_id,
                conversation_id,
                expected_revision,
                title,
            )
            .await
        {
            Ok(conversation) => {
                operation.succeeded().await?;
                Ok(conversation)
            },
            Err(error) => {
                operation.failed("database_mutation_failed").await?;
                Err(error)
            },
        }
    }

    /// Soft-deletes a conversation after delete authorization succeeds.
    pub async fn delete_conversation(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        expected_revision: i64,
    ) -> Result<()> {
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::DeleteConversation,
                    "conversation",
                    conversation_id,
                )
                .with_revision_id(expected_revision.to_string()),
                Permission::Delete,
                "conversation",
                conversation_id,
            )
            .await?;
        if let Err(error) = self
            .store
            .delete_conversation(tenant_id, conversation_id, expected_revision)
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        operation.succeeded().await
    }

    /// Persists one user message without starting a model response.
    pub async fn create_message(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        content: &str,
        attachments: &[MessageAttachment],
    ) -> Result<ConversationMessage> {
        let content = Self::validate_content(content, attachments.len())?;
        let message_id = Uuid::new_v4().simple().to_string();
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::CreateMessage,
                    "conversation_message",
                    &message_id,
                )
                .with_details(serde_json::json!({
                    "conversation_id": conversation_id,
                })),
                Permission::Edit,
                "conversation",
                conversation_id,
            )
            .await?;
        if let Err(error) = self
            .resolve_attachments(tenant_id, user_id, context, attachments)
            .await
        {
            operation.failed("attachment_validation_failed").await?;
            return Err(error);
        }
        let result = self
            .store
            .create_message(
                tenant_id,
                conversation_id,
                &NewConversationMessage {
                    message_id: &message_id,
                    role: MessageRole::User,
                    content,
                    model_alias: None,
                    status: MessageStatus::Completed,
                    created_by: user_id,
                    attachments,
                },
            )
            .await;
        match result {
            Ok(message) => {
                operation.succeeded().await?;
                Ok(message)
            },
            Err(error) => {
                operation.failed("database_mutation_failed").await?;
                Err(error)
            },
        }
    }

    /// Edits an owned user message and records its immutable next revision.
    #[expect(
        clippy::too_many_arguments,
        reason = "message edits require explicit actor and optimistic revision context"
    )]
    pub async fn update_message(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        message_id: &str,
        expected_revision: i64,
        content: &str,
        attachments: &[MessageAttachment],
    ) -> Result<ConversationMessage> {
        let content = Self::validate_content(content, attachments.len())?;
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::UpdateMessage,
                    "conversation_message",
                    message_id,
                )
                .with_revision_id(expected_revision.to_string())
                .with_details(serde_json::json!({
                    "conversation_id": conversation_id,
                })),
                Permission::Edit,
                "conversation",
                conversation_id,
            )
            .await?;
        if let Err(error) = self
            .resolve_attachments(tenant_id, user_id, context, attachments)
            .await
        {
            operation.failed("attachment_validation_failed").await?;
            return Err(error);
        }
        match self
            .store
            .update_message(
                tenant_id,
                conversation_id,
                message_id,
                expected_revision,
                content,
                attachments,
                user_id,
            )
            .await
        {
            Ok(message) => {
                operation.succeeded().await?;
                Ok(message)
            },
            Err(error) => {
                operation.failed("database_mutation_failed").await?;
                Err(error)
            },
        }
    }

    /// Soft-deletes an owned user message and keeps every prior revision.
    pub async fn delete_message(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        message_id: &str,
        expected_revision: i64,
    ) -> Result<()> {
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::DeleteMessage,
                    "conversation_message",
                    message_id,
                )
                .with_revision_id(expected_revision.to_string())
                .with_details(serde_json::json!({
                    "conversation_id": conversation_id,
                })),
                Permission::Edit,
                "conversation",
                conversation_id,
            )
            .await?;
        if let Err(error) = self
            .store
            .delete_message(
                tenant_id,
                conversation_id,
                message_id,
                expected_revision,
                user_id,
            )
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        operation.succeeded().await
    }

    /// Persists a user turn, streams Scribe, and persists its terminal result.
    #[expect(
        clippy::too_many_arguments,
        reason = "generation requests carry explicit actor and conversation context"
    )]
    pub async fn ask(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        conversation_id: &str,
        prompt: &str,
        model_alias: Option<&str>,
        attachments: &[MessageAttachment],
    ) -> Result<ConversationGeneration> {
        let prompt = Self::validate_content(prompt, attachments.len())?;
        let message_id = Uuid::new_v4().simple().to_string();
        let response_message_id = Uuid::new_v4().simple().to_string();
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditTarget::new(
                    AuditAction::ConversationalQuery,
                    "conversation",
                    conversation_id,
                )
                .with_details(serde_json::json!({
                    "message_id": message_id,
                    "response_message_id": response_message_id,
                })),
                Permission::Edit,
                "conversation",
                conversation_id,
            )
            .await?;
        let runtime_attachments = match self
            .resolve_attachments(tenant_id, user_id, context, attachments)
            .await
        {
            Ok(attachments) => attachments,
            Err(error) => {
                operation.failed("attachment_validation_failed").await?;
                return Err(error);
            },
        };
        let conversation = match self
            .store
            .begin_generation(
                tenant_id,
                conversation_id,
                &message_id,
                &NewConversationMessage {
                    message_id: &message_id,
                    role: MessageRole::User,
                    content: prompt,
                    model_alias,
                    status: MessageStatus::Pending,
                    created_by: user_id,
                    attachments,
                },
            )
            .await
        {
            Ok(conversation) => conversation,
            Err(error) => {
                operation.failed("database_mutation_failed").await?;
                return Err(error);
            },
        };
        let history = match self
            .resolve_history(
                tenant_id,
                user_id,
                context,
                &conversation.messages,
                &message_id,
            )
            .await
        {
            Ok(history) => history,
            Err(error) => {
                self.store
                    .fail_generation(
                        tenant_id,
                        conversation_id,
                        &message_id,
                        &message_id,
                        user_id,
                    )
                    .await?;
                operation.failed("history_resolution_failed").await?;
                return Err(error);
            },
        };
        let provenance = async {
            let mut sources = Vec::new();
            for attachment in attachments {
                sources.push(EvidenceSource {
                    resource_type: "raw".to_owned(),
                    resource_id: attachment.object_key.clone(),
                    manage: false,
                    entity_id: None,
                    modality: None,
                    state_type: None,
                });
            }
            for historical in &history {
                if let Some(message) =
                    conversation.messages.iter().find(|message| {
                        message.message_id == historical.message_id
                    })
                {
                    if let Some(evidence) = &message.evidence {
                        sources.extend(evidence.iter().cloned());
                    }
                    for attachment in &message.attachments {
                        sources.push(EvidenceSource {
                            resource_type: "raw".to_owned(),
                            resource_id: attachment.object_key.clone(),
                            manage: false,
                            entity_id: None,
                            modality: None,
                            state_type: None,
                        });
                    }
                }
            }
            self.store
                .record_generation_evidence(
                    tenant_id,
                    conversation_id,
                    &message_id,
                    &sources,
                )
                .await
        }
        .await;
        if let Err(error) = provenance {
            self.store
                .fail_generation(
                    tenant_id,
                    conversation_id,
                    &message_id,
                    &message_id,
                    user_id,
                )
                .await?;
            operation.failed("history_provenance_failed").await?;
            return Err(error);
        }
        let agent_stream = match self
            .agent
            .start(AgentRequest {
                tenant_id,
                user_id,
                conversation_id,
                message_id: &message_id,
                model_alias,
                prompt,
                history,
                attachments: runtime_attachments,
                authorization: context,
            })
            .await
        {
            Ok(stream) => stream,
            Err(error) => {
                self.store
                    .fail_generation(
                        tenant_id,
                        conversation_id,
                        &message_id,
                        &message_id,
                        user_id,
                    )
                    .await?;
                operation.failed("scribe_start_failed").await?;
                return Err(error);
            },
        };

        let (output_tx, mut output_rx) = mpsc::channel(256);
        let lagged = Arc::new(AtomicBool::new(false));
        let worker_lagged = Arc::clone(&lagged);
        let store = Arc::clone(&self.store);
        let tenant_id = tenant_id.to_owned();
        let user_id = user_id.to_owned();
        let conversation_id = conversation_id.to_owned();
        let generation_id = message_id.clone();
        let assistant_id = response_message_id.clone();
        let model_alias = model_alias.map(str::to_owned);
        std::mem::drop(tokio::spawn(async move {
            Self::persist_generation(
                store,
                operation,
                tenant_id,
                user_id,
                conversation_id,
                generation_id,
                assistant_id,
                model_alias,
                agent_stream,
                output_tx,
                worker_lagged,
            )
            .await;
        }));
        let stream = async_stream::stream! {
            while let Some(chunk) = output_rx.recv().await {
                yield chunk;
            }
            if lagged.load(Ordering::Acquire) { yield Err(anyhow::anyhow!("Subscriber fell behind; resume using generationEvents")); }
        };
        Ok(ConversationGeneration {
            message_id,
            response_message_id,
            stream: Box::pin(stream),
        })
    }

    /// Drains generation after client disconnect and persists exactly one
    /// terminal state.
    #[expect(
        clippy::too_many_arguments,
        reason = "terminal persistence requires the immutable request identity"
    )]
    async fn persist_generation(
        store: Arc<dyn ConversationStore>,
        operation: AuditOperation,
        tenant_id: String,
        user_id: String,
        conversation_id: String,
        generation_id: String,
        assistant_id: String,
        model_alias: Option<String>,
        mut agent_stream: crate::application::ports::conversation_agent::AgentStream,
        output_tx: mpsc::Sender<Result<AgentChunk>>,
        lagged: Arc<AtomicBool>,
    ) {
        let mut content = String::new();
        let mut batch = String::with_capacity(8192);
        let mut next_flush =
            tokio::time::Instant::now() + Duration::from_millis(100);
        let mut output_tx = Some(output_tx);
        let mut failure = None;
        loop {
            let chunk = tokio::select! {
                chunk = agent_stream.next() => chunk,
                () = tokio::time::sleep_until(next_flush), if !batch.is_empty() => {
                    if let Err(error) = store.append_generation_content(&tenant_id, &conversation_id, &generation_id, &batch).await {
                        failure = Some(error.to_string());
                        break;
                    }
                    batch.clear();
                    next_flush = tokio::time::Instant::now() + Duration::from_millis(100);
                    continue;
                }
            };
            let Some(chunk) = chunk else {
                break;
            };
            match chunk {
                Ok(chunk) => {
                    let AgentChunk::Content(fragment) = &chunk;
                    if content.len() + fragment.len() > 1024 * 1024 {
                        failure =
                            Some("Generation output exceeds limit".to_owned());
                        break;
                    }
                    content.push_str(fragment);
                    if batch.len() + fragment.len() > 65536 {
                        if let Err(error) = store
                            .append_generation_content(
                                &tenant_id,
                                &conversation_id,
                                &generation_id,
                                &batch,
                            )
                            .await
                        {
                            failure = Some(error.to_string());
                            break;
                        }
                        batch.clear();
                    }
                    batch.push_str(fragment);
                    if batch.len() >= 4096 {
                        if let Err(error) = store
                            .append_generation_content(
                                &tenant_id,
                                &conversation_id,
                                &generation_id,
                                &batch,
                            )
                            .await
                        {
                            failure = Some(error.to_string());
                            break;
                        }
                        batch.clear();
                        next_flush = tokio::time::Instant::now() +
                            Duration::from_millis(100);
                    }
                    if let Some(sender) = &output_tx &&
                        let Err(error) = sender.try_send(Ok(chunk))
                    {
                        if matches!(error, mpsc::error::TrySendError::Full(_))
                        {
                            lagged.store(true, Ordering::Release);
                        }
                        output_tx = None;
                        tracing::debug!(
                            event.name = "gateway.scribe.client_detached",
                            tenant_id,
                            conversation_id,
                            generation_id,
                            "Subscriber detached; persistence continues"
                        );
                    }
                },
                Err(error) => {
                    failure = Some(error.to_string());
                    if let Some(sender) = &output_tx &&
                        sender.try_send(Err(error)).is_err()
                    {
                        lagged.store(true, Ordering::Release);
                        tracing::debug!(
                            event.name =
                                "gateway.scribe.failure_client_detached",
                            tenant_id,
                            conversation_id,
                            generation_id,
                            "Scribe failure could not be delivered to detached client"
                        );
                    }
                    break;
                },
            }
        }
        if failure.is_none() &&
            !batch.is_empty() &&
            let Err(error) = store
                .append_generation_content(
                    &tenant_id,
                    &conversation_id,
                    &generation_id,
                    &batch,
                )
                .await
        {
            failure = Some(error.to_string());
        }
        if let Some(error) = failure {
            if let Err(persistence_error) = store
                .fail_generation(
                    &tenant_id,
                    &conversation_id,
                    &generation_id,
                    &generation_id,
                    &user_id,
                )
                .await
            {
                tracing::error!(
                    event.name = "gateway.scribe.failure_persistence_failed",
                    tenant_id,
                    conversation_id,
                    generation_id,
                    error = %persistence_error,
                    "failed to persist Scribe terminal failure"
                );
            }
            if let Err(audit_error) =
                operation.failed("scribe_generation_failed").await
            {
                tracing::error!(
                    event.name = "gateway.scribe.audit_failed",
                    tenant_id,
                    conversation_id,
                    generation_id,
                    error = %audit_error,
                    generation.error = %error,
                    "failed to persist Scribe failure audit"
                );
            }
            return;
        }

        let assistant = NewConversationMessage {
            message_id: &assistant_id,
            role: MessageRole::Assistant,
            content: &content,
            model_alias: model_alias.as_deref(),
            status: MessageStatus::Completed,
            created_by: &user_id,
            attachments: &[],
        };
        match store
            .complete_generation(
                &tenant_id,
                &conversation_id,
                &generation_id,
                &assistant,
            )
            .await
        {
            Ok(()) => {
                if let Err(error) = operation.succeeded().await {
                    tracing::error!(
                        event.name = "gateway.scribe.audit_failed",
                        tenant_id,
                        conversation_id,
                        generation_id,
                        error = %error,
                        "failed to persist Scribe success audit"
                    );
                }
            },
            Err(error) => {
                tracing::error!(
                    event.name = "gateway.scribe.completion_persistence_failed",
                    tenant_id,
                    conversation_id,
                    generation_id,
                    error = %error,
                    "failed to persist Scribe completion"
                );
                if let Err(recovery_error) = store
                    .fail_generation(
                        &tenant_id,
                        &conversation_id,
                        &generation_id,
                        &generation_id,
                        &user_id,
                    )
                    .await
                {
                    tracing::error!(
                        event.name =
                            "gateway.scribe.completion_recovery_failed",
                        tenant_id,
                        conversation_id,
                        generation_id,
                        error = %recovery_error,
                        "failed to release Scribe generation reservation"
                    );
                }
                if let Err(audit_error) =
                    operation.failed("completion_persistence_failed").await
                {
                    tracing::error!(
                        event.name = "gateway.scribe.audit_failed",
                        tenant_id,
                        conversation_id,
                        generation_id,
                        error = %audit_error,
                        "failed to persist completion failure audit"
                    );
                }
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    use anyhow::{Result, anyhow, ensure};
    use futures::stream;

    use super::*;
    use crate::application::ports::attachment_store::AttachmentStore;
    use crate::application::ports::conversation_agent::{
        AgentAttachment, AgentStream,
    };
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization, audit, identity,
    };

    struct FastAgent;

    #[tokio::test]
    async fn historical_evidence_is_rechecked_against_current_permissions()
    -> Result<()> {
        let (audit, _) = audit();
        let service = ConversationService::new(
            Arc::new(MemoryConversationStore::default()),
            Arc::new(MemoryAgent),
            Arc::new(MemoryAttachmentStore),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            audit,
        );
        let source =
            crate::application::ports::conversation_store::EvidenceSource {
                resource_type: "event".to_owned(),
                resource_id: "evt_private".to_owned(),
                manage: false,
                entity_id: None,
                modality: None,
                state_type: None,
            };
        ensure!(
            !service
                .sources_visible(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    &[source]
                )
                .await?
        );
        Ok(())
    }

    #[tokio::test]
    async fn model_history_excludes_unprovenanced_answers_and_other_actors()
    -> Result<()> {
        let (audit, _) = audit();
        let service = ConversationService::new(
            Arc::new(MemoryConversationStore::default()),
            Arc::new(MemoryAgent),
            Arc::new(MemoryAttachmentStore),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        );
        let messages = [
            (MessageRole::User, "user_a"),
            (MessageRole::Assistant, "user_a"),
            (MessageRole::User, "user_b"),
        ]
        .map(|(role, actor)| {
            MemoryConversationStore::message(&NewConversationMessage {
                message_id: "old",
                role,
                content: "old evidence",
                model_alias: None,
                status: MessageStatus::Completed,
                created_by: actor,
                attachments: &[],
            })
        });
        let history = service
            .resolve_history(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                &messages,
                "pending",
            )
            .await?;
        ensure!(history.len() == 1);
        ensure!(
            history
                .first()
                .is_some_and(|message| message.role == MessageRole::User)
        );
        Ok(())
    }

    #[async_trait::async_trait]
    impl ConversationAgent for FastAgent {
        async fn start(
            &self,
            _request: AgentRequest<'_>,
        ) -> Result<AgentStream> {
            Ok(Box::pin(stream::iter(
                (0..300).map(|_| Ok(AgentChunk::Content("x".to_owned()))),
            )))
        }
    }

    #[tokio::test]
    async fn slow_subscriber_does_not_block_generation_persistence()
    -> Result<()> {
        let store = Arc::new(MemoryConversationStore::default());
        let (audit, _) = audit();
        let service = ConversationService::new(
            store.clone(),
            Arc::new(FastAgent),
            Arc::new(MemoryAttachmentStore),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        );
        let context = QueryContext::default();
        let conversation = service
            .create_conversation("tenant_a", "user_a", &context, "Chat")
            .await?;
        let generation = service
            .ask(
                "tenant_a",
                "user_a",
                &context,
                &conversation.conversation_id,
                "question",
                None,
                &[],
            )
            .await?;
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let current = store
                    .get_conversation(
                        "tenant_a",
                        &conversation.conversation_id,
                        false,
                    )
                    .await?
                    .context("Missing conversation")?;
                if current.active_generation_id.is_none() {
                    return Ok::<_, anyhow::Error>(());
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .context("Slow subscriber blocked persistence")??;
        drop(generation);
        Ok(())
    }

    #[derive(Default)]
    struct MemoryConversationStore {
        conversation: Mutex<Option<Conversation>>,
        reject_completion: AtomicBool,
        failed_generations: AtomicUsize,
    }

    impl MemoryConversationStore {
        /// Locks the test projection without hiding poisoning failures.
        fn lock(
            &self,
        ) -> Result<std::sync::MutexGuard<'_, Option<Conversation>>> {
            self.conversation.lock().map_err(|error| {
                anyhow!("conversation test lock poisoned: {error}")
            })
        }

        /// Creates the current message projection from one persistence input.
        fn message(input: &NewConversationMessage<'_>) -> ConversationMessage {
            ConversationMessage {
                message_id: input.message_id.to_owned(),
                role: input.role,
                content: input.content.to_owned(),
                model_alias: input.model_alias.map(str::to_owned),
                status: input.status,
                revision: 1,
                created_by: input.created_by.to_owned(),
                attachments: input.attachments.to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                deleted_at_ms: None,
                evidence: None,
            }
        }
    }

    #[async_trait::async_trait]
    impl ConversationStore for MemoryConversationStore {
        async fn record_generation_evidence(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            generation_id: &str,
            sources: &[crate::application::ports::conversation_store::EvidenceSource],
        ) -> Result<()> {
            let mut state = self.lock()?;
            if let Some(message) = state.as_mut().and_then(|conversation| {
                conversation
                    .messages
                    .iter_mut()
                    .find(|message| message.message_id == generation_id)
            }) {
                message.evidence = Some(sources.to_vec());
            }
            Ok(())
        }

        async fn append_generation_content(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            _generation_id: &str,
            _content: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn generation_events(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            _generation_id: &str,
            _after: i32,
            _limit: usize,
        ) -> Result<
            Vec<
                crate::application::ports::conversation_store::GenerationEvent,
            >,
        > {
            Ok(Vec::new())
        }

        async fn create_conversation(
            &self,
            _tenant_id: &str,
            input: &NewConversation<'_>,
        ) -> Result<Conversation> {
            let conversation = Conversation {
                conversation_id: input.conversation_id.to_owned(),
                owner_id: input.owner_id.to_owned(),
                title: input.title.to_owned(),
                revision: 0,
                active_generation_id: None,
                messages: Vec::new(),
                created_at_ms: 1,
                updated_at_ms: 1,
                deleted_at_ms: None,
            };
            *self.lock()? = Some(conversation.clone());
            Ok(conversation)
        }

        async fn list_conversations(
            &self,
            _tenant_id: &str,
            _limit: usize,
        ) -> Result<Vec<Conversation>> {
            Ok(self
                .lock()?
                .iter()
                .filter(|conversation| conversation.deleted_at_ms.is_none())
                .cloned()
                .collect())
        }

        async fn get_conversation(
            &self,
            _tenant_id: &str,
            conversation_id: &str,
            _include_deleted_messages: bool,
        ) -> Result<Option<Conversation>> {
            Ok(self
                .lock()?
                .as_ref()
                .filter(|conversation| {
                    conversation.conversation_id == conversation_id
                })
                .cloned())
        }

        async fn update_conversation(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            expected_revision: i64,
            title: &str,
        ) -> Result<Conversation> {
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            ensure!(
                conversation.revision == expected_revision,
                "stale revision"
            );
            conversation.title = title.to_owned();
            conversation.revision += 1;
            Ok(conversation.clone())
        }

        async fn delete_conversation(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            expected_revision: i64,
        ) -> Result<()> {
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            ensure!(
                conversation.revision == expected_revision,
                "stale revision"
            );
            conversation.deleted_at_ms = Some(2);
            conversation.revision += 1;
            Ok(())
        }

        async fn create_message(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            input: &NewConversationMessage<'_>,
        ) -> Result<ConversationMessage> {
            let message = Self::message(input);
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            conversation.messages.push(message.clone());
            conversation.revision += 1;
            Ok(message)
        }

        async fn update_message(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            message_id: &str,
            expected_revision: i64,
            content: &str,
            attachments: &[MessageAttachment],
            changed_by: &str,
        ) -> Result<ConversationMessage> {
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            let message = conversation
                .messages
                .iter_mut()
                .find(|message| message.message_id == message_id)
                .context("message missing")?;
            ensure!(message.revision == expected_revision, "stale revision");
            ensure!(message.created_by == changed_by, "message owner changed");
            message.content = content.to_owned();
            message.attachments = attachments.to_vec();
            message.revision += 1;
            conversation.revision += 1;
            Ok(message.clone())
        }

        async fn delete_message(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            message_id: &str,
            expected_revision: i64,
            changed_by: &str,
        ) -> Result<()> {
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            let message = conversation
                .messages
                .iter_mut()
                .find(|message| message.message_id == message_id)
                .context("message missing")?;
            ensure!(message.revision == expected_revision, "stale revision");
            ensure!(message.created_by == changed_by, "message owner changed");
            message.deleted_at_ms = Some(2);
            message.revision += 1;
            conversation.revision += 1;
            Ok(())
        }

        async fn begin_generation(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            generation_id: &str,
            input: &NewConversationMessage<'_>,
        ) -> Result<Conversation> {
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            ensure!(
                conversation.active_generation_id.is_none(),
                "generation active"
            );
            conversation.active_generation_id = Some(generation_id.to_owned());
            conversation.messages.push(Self::message(input));
            conversation.revision += 1;
            Ok(conversation.clone())
        }

        async fn complete_generation(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            generation_id: &str,
            input: &NewConversationMessage<'_>,
        ) -> Result<()> {
            tokio::task::yield_now().await;
            if self.reject_completion.load(Ordering::Acquire) {
                return Err(anyhow!("completion persistence failed"));
            }
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            let pending = conversation
                .messages
                .iter_mut()
                .find(|message| message.message_id == generation_id)
                .context("pending message missing")?;
            pending.status = MessageStatus::Completed;
            pending.revision += 1;
            let evidence = pending.evidence.clone().unwrap_or_default();
            let mut assistant = Self::message(input);
            assistant.evidence = Some(evidence);
            conversation.messages.push(assistant);
            conversation.active_generation_id = None;
            conversation.revision += 1;
            Ok(())
        }

        async fn fail_generation(
            &self,
            _tenant_id: &str,
            _conversation_id: &str,
            generation_id: &str,
            message_id: &str,
            _changed_by: &str,
        ) -> Result<()> {
            self.failed_generations.fetch_add(1, Ordering::AcqRel);
            let mut state = self.lock()?;
            let conversation =
                state.as_mut().context("conversation missing")?;
            ensure!(
                conversation.active_generation_id.as_deref() ==
                    Some(generation_id),
                "generation changed"
            );
            let pending = conversation
                .messages
                .iter_mut()
                .find(|message| message.message_id == message_id)
                .context("pending message missing")?;
            pending.status = MessageStatus::Failed;
            pending.revision += 1;
            conversation.active_generation_id = None;
            conversation.revision += 1;
            Ok(())
        }
    }

    struct MemoryAgent;

    #[async_trait::async_trait]
    impl ConversationAgent for MemoryAgent {
        async fn start(
            &self,
            _request: AgentRequest<'_>,
        ) -> Result<AgentStream> {
            Ok(Box::pin(stream::iter([
                Ok(AgentChunk::Content("ans".to_owned())),
                Ok(AgentChunk::Content("wer".to_owned())),
            ])))
        }
    }

    struct StreamFailureAgent;

    #[async_trait::async_trait]
    impl ConversationAgent for StreamFailureAgent {
        async fn start(
            &self,
            _request: AgentRequest<'_>,
        ) -> Result<AgentStream> {
            Ok(Box::pin(stream::iter([Err(anyhow!("generation failed"))])))
        }
    }

    struct StartFailureAgent;

    #[async_trait::async_trait]
    impl ConversationAgent for StartFailureAgent {
        async fn start(
            &self,
            _request: AgentRequest<'_>,
        ) -> Result<AgentStream> {
            Err(anyhow!("model unavailable"))
        }
    }

    struct MemoryAttachmentStore;

    #[async_trait::async_trait]
    impl AttachmentStore for MemoryAttachmentStore {
        async fn resolve_for_scribe(
            &self,
            _tenant_id: &str,
            _user_id: &str,
            attachments: &[MessageAttachment],
            _expires_in: Duration,
        ) -> Result<Vec<AgentAttachment>> {
            Ok(attachments
                .iter()
                .map(|attachment| AgentAttachment {
                    kind: attachment.kind,
                    url: format!("https://s3/{}", attachment.object_key),
                })
                .collect())
        }
    }

    #[test]
    fn message_validation_accepts_attachment_only_and_rejects_empty() {
        assert!(ConversationService::validate_content("", 1).is_ok());
        assert!(ConversationService::validate_content("", 0).is_err());
        assert!(
            ConversationService::validate_content("ok", MAX_ATTACHMENTS + 1)
                .is_err()
        );
    }

    #[test]
    fn conversation_titles_are_trimmed_and_bounded() -> Result<()> {
        assert_eq!(ConversationService::validate_title("  title  ")?, "title");
        assert!(ConversationService::validate_title("").is_err());
        assert!(
            ConversationService::validate_title(
                &"x".repeat(MAX_TITLE_BYTES + 1)
            )
            .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn authorized_conversation_lifecycle_streams_after_durable_completion()
    -> Result<()> {
        let store = Arc::new(MemoryConversationStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, audit_store) = audit();
        let service = ConversationService::new(
            store.clone(),
            Arc::new(MemoryAgent),
            Arc::new(MemoryAttachmentStore),
            identity(true),
            authorization.clone(),
            audit,
        );
        let context = QueryContext {
            request_id: "request-conversation".to_owned(),
            ..QueryContext::default()
        };
        let created = service
            .create_conversation("tenant_a", "user_a", &context, "Chat")
            .await?;
        ensure!(
            service
                .conversations("tenant_a", "user_a", &context, 10)
                .await?
                .len() ==
                1
        );
        let updated = service
            .update_conversation(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                created.revision,
                "Updated chat",
            )
            .await?;
        let attachment = MessageAttachment {
            object_key: "tenant_a/raw/default/image.png".to_owned(),
            kind: crate::application::ports::conversation_store::AttachmentKind::Image,
            file_name: Some("image.png".to_owned()),
            content_type: Some("image/png".to_owned()),
            size_bytes: Some(20),
        };
        let message = service
            .create_message(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                "hello",
                std::slice::from_ref(&attachment),
            )
            .await?;
        let edited = service
            .update_message(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                &message.message_id,
                message.revision,
                "edited",
                &[],
            )
            .await?;
        service
            .delete_message(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                &edited.message_id,
                edited.revision,
            )
            .await?;
        let generation = service
            .ask(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                "question",
                Some("writer"),
                &[],
            )
            .await?;
        let chunks = generation.stream.collect::<Vec<_>>().await;
        ensure!(chunks.len() == 2);
        ensure!(chunks.iter().all(Result::is_ok));
        let persisted = service
            .conversation(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                true,
            )
            .await?
            .context("conversation missing after generation")?;
        ensure!(persisted.active_generation_id.is_none());
        ensure!(persisted.messages.iter().any(|candidate| {
            candidate.message_id == generation.response_message_id &&
                candidate.content == "answer" &&
                candidate.status == MessageStatus::Completed
        }));
        service
            .delete_conversation(
                "tenant_a",
                "user_a",
                &context,
                &created.conversation_id,
                persisted.revision,
            )
            .await?;

        ensure!(updated.title == "Updated chat");
        ensure!(
            authorization
                .mutations
                .lock()
                .map_err(|error| anyhow!(
                    "authorization test lock poisoned: {error}"
                ))?
                .len() ==
                2
        );
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 14);
        ensure!(
            events
                .iter()
                .all(|event| event.request_id == "request-conversation")
        );
        Ok(())
    }

    #[tokio::test]
    async fn scribe_failures_release_reservations_and_persist_failed_messages()
    -> Result<()> {
        for (agent, starts_stream) in [
            (
                Arc::new(StreamFailureAgent) as Arc<dyn ConversationAgent>,
                true,
            ),
            (
                Arc::new(StartFailureAgent) as Arc<dyn ConversationAgent>,
                false,
            ),
        ] {
            let store = Arc::new(MemoryConversationStore::default());
            let (audit, audit_store) = audit();
            let service = ConversationService::new(
                store,
                agent,
                Arc::new(MemoryAttachmentStore),
                identity(true),
                TestAuthorization::new(AuthorizationDecision::Allow),
                audit,
            );
            let context = QueryContext {
                request_id: "request-failure".to_owned(),
                ..QueryContext::default()
            };
            let conversation = service
                .create_conversation("tenant_a", "user_a", &context, "Chat")
                .await?;
            let result = service
                .ask(
                    "tenant_a",
                    "user_a",
                    &context,
                    &conversation.conversation_id,
                    "question",
                    None,
                    &[],
                )
                .await;
            if starts_stream {
                let chunks = result?.stream.collect::<Vec<_>>().await;
                ensure!(chunks.len() == 1);
                ensure!(chunks.first().is_some_and(Result::is_err));
            } else {
                ensure!(result.is_err());
            }
            let persisted = service
                .conversation(
                    "tenant_a",
                    "user_a",
                    &context,
                    &conversation.conversation_id,
                    false,
                )
                .await?
                .context("conversation missing after failure")?;
            ensure!(persisted.active_generation_id.is_none());
            ensure!(persisted.messages.iter().any(|message| {
                message.role == MessageRole::User &&
                    message.status == MessageStatus::Failed
            }));
            let events = audit_store.events.lock().map_err(|error| {
                anyhow!("audit test lock poisoned: {error}")
            })?;
            ensure!(events.len() == 4);
            ensure!(
                events
                    .last()
                    .and_then(|event| event.failure_kind.as_deref()) ==
                    Some(if starts_stream {
                        "scribe_generation_failed"
                    } else {
                        "scribe_start_failed"
                    })
            );
        }
        Ok(())
    }

    #[tokio::test]
    async fn completion_persistence_failure_releases_generation_reservation()
    -> Result<()> {
        let store = Arc::new(MemoryConversationStore::default());
        store.reject_completion.store(true, Ordering::Release);
        let (audit, audit_store) = audit();
        let service = ConversationService::new(
            store.clone(),
            Arc::new(MemoryAgent),
            Arc::new(MemoryAttachmentStore),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        );
        let context = QueryContext {
            request_id: "request-completion-failure".to_owned(),
            ..QueryContext::default()
        };
        let conversation = service
            .create_conversation("tenant_a", "user_a", &context, "Chat")
            .await?;
        let generation = service
            .ask(
                "tenant_a",
                "user_a",
                &context,
                &conversation.conversation_id,
                "question",
                None,
                &[],
            )
            .await?;
        let chunks = generation.stream.collect::<Vec<_>>().await;
        ensure!(chunks.len() == 2);

        let persisted = store
            .get_conversation("tenant_a", &conversation.conversation_id, false)
            .await?
            .context("conversation missing after completion failure")?;
        ensure!(persisted.active_generation_id.is_none());
        ensure!(
            persisted.messages.iter().any(|message| {
                message.message_id == generation.message_id &&
                    message.status == MessageStatus::Failed
            }),
            "pending user message was not marked failed"
        );
        ensure!(store.failed_generations.load(Ordering::Acquire) == 1);
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(
            events
                .last()
                .and_then(|event| event.failure_kind.as_deref()) ==
                Some("completion_persistence_failed")
        );
        Ok(())
    }
}
