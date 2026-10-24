//! GraphQL schema definition mapping to application use cases with FGAC.

use std::pin::Pin;
use std::sync::Arc;

use futures::{Stream, StreamExt as _};
use juniper::{
    FieldError, FieldResult, RootNode, graphql_object, graphql_scalar,
    graphql_subscription,
};
use serde_json::Value;

use super::{administration as admin_types, capabilities, pengolo};
use crate::adapters::inbound::graphql::context::AppContext;
use crate::application::ports::audit_store::{
    AuditEvent, AuditFilter, AuditOutcome,
};
use crate::application::ports::control_plane_store::{
    OntologyCatalogEntry, OntologyPublication, PipelineExecution,
    PipelineOntologyBinding,
};
use crate::application::ports::conversation_agent::{
    AgentChunk, AgentModelCatalog,
};
use crate::application::ports::conversation_store::{
    AttachmentKind, Conversation, ConversationMessage, MessageAttachment,
};
use crate::application::ports::iam_store::{IamRole, IamUser, RoleAssignment};
use crate::application::ports::pipeline_store::PipelineDefinition;
use crate::application::usecases::iam_admin::{
    DataDomainGrant, DataDomainGrantRequest,
};
use crate::application::usecases::search::{
    GlobalSearchHit, StructuredSearchQuery,
};
use crate::application::usecases::uploads::UploadDestination;

fn pengolo_actor(
    ctx: &AppContext,
) -> FieldResult<crate::application::usecases::pengolo::Principal<'_>> {
    ctx.verify_authentication()?;
    Ok(crate::application::usecases::pengolo::Principal {
        tenant: &ctx.tenant_id,
        user: &ctx.user_id,
        context: &ctx.authz_context,
    })
}

fn pengolo_result<T: serde::Serialize, U: serde::de::DeserializeOwned>(
    result: anyhow::Result<T>,
) -> FieldResult<U> {
    result
        .and_then(|value| serde_json::to_value(value).map_err(Into::into))
        .and_then(|value| serde_json::from_value(value).map_err(Into::into))
        .map_err(|_| {
            tracing::warn!(event.name = "pengolo.request.refused");
            FieldError::new(
                "Pengolo resource unavailable or request rejected",
                juniper::graphql_value!({"code":"REQUEST_FAILED"}),
            )
        })
}

fn pengolo_input<T: serde::Serialize>(input: T) -> FieldResult<Value> {
    serde_json::to_value(input)
        .map_err(|_| FieldError::from("Invalid Pengolo input"))
}

/// A custom GraphQL scalar to represent dynamic JSON objects.
///
/// We deliberately accept JSON as a string in GraphQL inputs to avoid
/// ambiguous input coercions, but output uses Juniper's displayable scalar
/// conversion.
#[derive(Debug, Clone)]
#[graphql_scalar(
    name = "JSON",
    description = "Dynamic JSON scalar for heterogeneous data",
    with = dynamic_json_scalar
)]
pub struct DynamicJson(pub Value);

#[derive(Debug, Clone, Copy, juniper::GraphQLEnum)]
#[graphql(name = "DataDomainGrant")]
pub enum GqlDataDomainGrant {
    Ingest,
    Read,
}

impl From<GqlDataDomainGrant> for DataDomainGrant {
    fn from(value: GqlDataDomainGrant) -> Self {
        match value {
            GqlDataDomainGrant::Ingest => Self::Ingest,
            GqlDataDomainGrant::Read => Self::Read,
        }
    }
}

mod dynamic_json_scalar {
    use juniper::{ParseScalarResult, ScalarToken, ScalarValue};

    use super::DynamicJson;

    /// Converts a JSON value into Juniper's configured output scalar.
    pub fn to_output<S: ScalarValue>(v: &DynamicJson) -> S {
        S::from_displayable_non_static(&v.0)
    }

    /// Parses JSON from the schema's explicit string input representation.
    pub fn from_input<S: ScalarValue>(
        v: &juniper::Scalar<S>,
    ) -> Result<DynamicJson, String> {
        v.try_as_str()
            .ok_or_else(|| format!("Expected a string for JSON, found: {}", v))
            .and_then(|s| serde_json::from_str(s).map_err(|e| e.to_string()))
            .map(DynamicJson)
    }

    /// Parses one scalar token as the JSON string representation.
    pub fn parse_token<S: ScalarValue>(
        value: ScalarToken<'_>,
    ) -> ParseScalarResult<S> {
        <String as juniper::ParseScalarValue<S>>::from_str(value)
    }
}

/// Search hit result (permission-filtered).
pub struct GqlSearchHit {
    entity_id: String,
    metadata: Value,
}

#[graphql_object(name = "SearchHit", context = AppContext)]
impl GqlSearchHit {
    /// Returns the tenant-local entity identifier.
    fn entity_id(&self) -> &str {
        &self.entity_id
    }

    /// Returns the current state metadata associated with the hit.
    fn metadata(&self) -> DynamicJson {
        DynamicJson(self.metadata.clone())
    }
}

/// Global search hit (union-like object).
///
/// We avoid GraphQL unions to keep the client experience simple (Palantir-like
/// “single table” results). The client can branch on `kind`.
pub struct GqlGlobalSearchHit {
    kind: String,

    entity_id: Option<String>,

    event_id: Option<String>,
    event_type: Option<String>,

    modality: Option<String>,

    /// Unix milliseconds encoded as f64 to satisfy Juniper scalar support.
    created_at_ms: Option<f64>,
    event_time_ms: Option<f64>,

    /// Embedding distance/similarity score (f64 for Juniper).
    score: Option<f64>,

    payload: Value,
}

#[derive(juniper::GraphQLObject)]
#[graphql(name = "SearchPage", context = AppContext)]
pub struct GqlSearchPage {
    items: Vec<GqlGlobalSearchHit>,
    after: Option<String>,
    navigation: Vec<GqlNavigationHit>,
}

#[derive(juniper::GraphQLObject)]
#[graphql(name = "NavigationHit", context = AppContext)]
pub struct GqlNavigationHit {
    id: String,
    kind: String,
    href: String,
    label: String,
    description: String,
    score: i32,
}

#[derive(juniper::GraphQLInputObject)]
#[graphql(name = "StructuredSearchInput")]
pub struct GqlStructuredSearchInput {
    pub text: Option<String>,
    pub entity_id: Option<String>,
    pub event_type: Option<String>,
    pub modality: Option<String>,
    pub limit: Option<i32>,
}

#[graphql_object(name = "GlobalSearchHit", context = AppContext)]
impl GqlGlobalSearchHit {
    /// Returns the stable variant discriminator.
    fn kind(&self) -> &str {
        &self.kind
    }

    /// Returns the entity identifier for entity and embedding hits.
    fn entity_id(&self) -> Option<&str> {
        self.entity_id.as_deref()
    }

    /// Returns the event identifier for event hits.
    fn event_id(&self) -> Option<&str> {
        self.event_id.as_deref()
    }

    /// Returns the event type for event hits.
    fn event_type(&self) -> Option<&str> {
        self.event_type.as_deref()
    }

    /// Returns the embedding modality for embedding hits.
    fn modality(&self) -> Option<&str> {
        self.modality.as_deref()
    }

    /// Returns creation time in Unix milliseconds when available.
    fn created_at_ms(&self) -> Option<f64> {
        self.created_at_ms
    }

    /// Returns event time in Unix milliseconds when available.
    fn event_time_ms(&self) -> Option<f64> {
        self.event_time_ms
    }

    /// Returns the embedding score when available.
    fn score(&self) -> Option<f64> {
        self.score
    }

    /// Returns the domain payload for the selected hit variant.
    fn payload(&self) -> DynamicJson {
        DynamicJson(self.payload.clone())
    }
}

/// Graph node for relations results.
pub struct GqlGraphNode {
    id: String,
    label: String,
    properties: Value,
    ontology_ref: Option<GqlOntologyReference>,
}

pub struct GqlOntologyReference(
    crate::application::ports::relations_store::OntologyReference,
);

#[graphql_object(name = "GraphOntologyReference", context = AppContext)]
impl GqlOntologyReference {
    fn tenant_id(&self) -> &str {
        &self.0.tenant_id
    }

    fn ontology_id(&self) -> &str {
        &self.0.ontology_id
    }

    fn revision_id(&self) -> &str {
        &self.0.revision_id
    }

    fn resource_id(&self) -> &str {
        &self.0.resource_id
    }

    fn resource_kind(&self) -> &str {
        &self.0.resource_kind
    }
}

#[graphql_object(name = "GraphNode", context = AppContext)]
impl GqlGraphNode {
    /// Returns the graph node identifier.
    fn id(&self) -> &str {
        &self.id
    }

    /// Returns the graph node label.
    fn label(&self) -> &str {
        &self.label
    }

    /// Returns the structural role independently of business classification.
    fn kind(&self) -> &str {
        &self.label
    }

    /// Returns immutable Registry provenance when this projection permits it.
    fn ontology_ref(&self) -> Option<&GqlOntologyReference> {
        self.ontology_ref.as_ref()
    }

    /// Returns the graph node properties.
    fn properties(&self) -> DynamicJson {
        DynamicJson(self.properties.clone())
    }
}

/// Graph edge for relations results.
pub struct GqlGraphEdge {
    from_id: String,
    to_id: String,
    label: String,
    properties: Value,
}

#[graphql_object(name = "GraphEdge", context = AppContext)]
impl GqlGraphEdge {
    /// Returns the source node identifier.
    #[graphql(name = "fromId")]
    fn source_id(&self) -> &str {
        &self.from_id
    }

    /// Returns the destination node identifier.
    fn to_id(&self) -> &str {
        &self.to_id
    }

    /// Returns the graph edge label.
    fn label(&self) -> &str {
        &self.label
    }

    /// Returns the graph edge properties.
    fn properties(&self) -> DynamicJson {
        DynamicJson(self.properties.clone())
    }
}

pub struct GqlGraphSubgraph {
    nodes: Vec<GqlGraphNode>,
    edges: Vec<GqlGraphEdge>,
}

#[derive(Debug, Clone, Copy, juniper::GraphQLEnum)]
#[graphql(name = "AttachmentKind")]
pub enum GqlAttachmentKind {
    Image,
    Audio,
    Document,
}

/// Cursor batches expose persisted content and terminal state for
/// reconnection.
#[derive(juniper::GraphQLObject)]
pub struct GqlGenerationEvent {
    sequence: i32,
    kind: String,
    content: String,
}

impl From<GqlAttachmentKind> for AttachmentKind {
    /// Converts the public enum into the canonical persistence value.
    fn from(value: GqlAttachmentKind) -> Self {
        match value {
            GqlAttachmentKind::Image => Self::Image,
            GqlAttachmentKind::Audio => Self::Audio,
            GqlAttachmentKind::Document => Self::Document,
        }
    }
}

#[derive(juniper::GraphQLInputObject)]
#[graphql(name = "MessageAttachmentInput")]
pub struct GqlMessageAttachmentInput {
    pub object_key: String,
    pub kind: GqlAttachmentKind,
    pub file_name: Option<String>,
    pub content_type: Option<String>,
    pub size_bytes: Option<i32>,
}

#[derive(juniper::GraphQLObject)]
#[graphql(name = "ScribeModel")]
pub struct GqlScribeModel {
    alias: String,
    provider: String,
    model: String,
    reasoning_efforts: Vec<String>,
    default_reasoning_effort: Option<String>,
}

#[derive(juniper::GraphQLObject)]
#[graphql(name = "ScribeModelCatalog")]
pub struct GqlScribeModelCatalog {
    default_model: Option<String>,
    models: Vec<GqlScribeModel>,
}

impl From<AgentModelCatalog> for GqlScribeModelCatalog {
    fn from(catalog: AgentModelCatalog) -> Self {
        Self {
            default_model: catalog.default_model,
            models: catalog
                .models
                .into_iter()
                .map(|model| GqlScribeModel {
                    alias: model.alias,
                    provider: model.provider,
                    model: model.model,
                    reasoning_efforts: model.reasoning_efforts,
                    default_reasoning_effort: model.default_reasoning_effort,
                })
                .collect(),
        }
    }
}

pub struct GqlMessageAttachment(MessageAttachment);

#[graphql_object(name = "MessageAttachment", context = AppContext)]
impl GqlMessageAttachment {
    /// Returns the durable S3 object key, never a reusable signed URL.
    fn object_key(&self) -> &str {
        &self.0.object_key
    }

    /// Returns the explicit media kind supported by Scribe.
    fn kind(&self) -> GqlAttachmentKind {
        match self.0.kind {
            AttachmentKind::Image => GqlAttachmentKind::Image,
            AttachmentKind::Audio => GqlAttachmentKind::Audio,
            AttachmentKind::Document => GqlAttachmentKind::Document,
        }
    }

    /// Returns the original display name when supplied.
    fn file_name(&self) -> Option<&str> {
        self.0.file_name.as_deref()
    }

    /// Returns the S3-verified media type when supplied.
    fn content_type(&self) -> Option<&str> {
        self.0.content_type.as_deref()
    }

    /// Returns the bounded attachment size as a GraphQL float.
    fn size_bytes(&self) -> Option<f64> {
        self.0.size_bytes.map(|size| size as f64)
    }
}

pub struct GqlConversationMessage(ConversationMessage);

#[graphql_object(name = "ConversationMessage", context = AppContext)]
impl GqlConversationMessage {
    /// Returns the immutable message identifier.
    fn message_id(&self) -> &str {
        &self.0.message_id
    }

    /// Returns the canonical participant role.
    fn role(&self) -> &str {
        self.0.role.as_str()
    }

    /// Returns the current message content projection.
    fn content(&self) -> &str {
        &self.0.content
    }

    /// Returns the model alias used for an AI turn when available.
    fn model_alias(&self) -> Option<&str> {
        self.0.model_alias.as_deref()
    }

    /// Returns the terminal or pending message status.
    fn status(&self) -> &str {
        self.0.status.as_str()
    }

    /// Returns the optimistic revision as a lossless decimal string.
    fn revision(&self) -> String {
        self.0.revision.to_string()
    }

    /// Returns the principal that caused the message to be persisted.
    fn created_by(&self) -> &str {
        &self.0.created_by
    }

    /// Returns durable attachment metadata without signed URLs.
    fn attachments(&self) -> Vec<GqlMessageAttachment> {
        self.0
            .attachments
            .iter()
            .cloned()
            .map(GqlMessageAttachment)
            .collect()
    }

    /// Returns message creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }

    /// Returns the most recent update time in Unix milliseconds.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }

    /// Returns soft-deletion time for explicitly requested history.
    fn deleted_at_ms(&self) -> Option<f64> {
        self.0.deleted_at_ms.map(i64_ms_to_f64)
    }
}

pub struct GqlConversation(Conversation);

#[graphql_object(name = "Conversation", context = AppContext)]
impl GqlConversation {
    /// Returns the immutable conversation identifier.
    fn conversation_id(&self) -> &str {
        &self.0.conversation_id
    }

    /// Returns the principal that owns the conversation.
    fn owner_id(&self) -> &str {
        &self.0.owner_id
    }

    /// Returns the current human-readable title.
    fn title(&self) -> &str {
        &self.0.title
    }

    /// Returns the optimistic revision as a lossless decimal string.
    fn revision(&self) -> String {
        self.0.revision.to_string()
    }

    /// Returns an active user message ID while Scribe is generating.
    fn active_generation_id(&self) -> Option<&str> {
        self.0.active_generation_id.as_deref()
    }

    /// Returns the current ordered message projection.
    fn messages(&self) -> Vec<GqlConversationMessage> {
        self.0
            .messages
            .iter()
            .cloned()
            .map(GqlConversationMessage)
            .collect()
    }

    /// Returns conversation creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }

    /// Returns the most recent update time in Unix milliseconds.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }
}

pub struct GqlPipelineDefinition(PipelineDefinition);

#[derive(juniper::GraphQLObject)]
#[graphql(name = "PipelineComposition")]
pub struct GqlPipelineComposition {
    definition: DynamicJson,
    blocks: i32,
    transfers: i32,
}

#[graphql_object(name = "PipelineDefinition", context = AppContext)]
impl GqlPipelineDefinition {
    /// Returns the tenant-unique pipeline identifier.
    fn pipeline_id(&self) -> &str {
        &self.0.pipeline_id
    }

    /// Returns the canonical name used by `platform/pipeline`.
    fn name(&self) -> &str {
        &self.0.name
    }

    /// Returns the principal that owns the pipeline.
    fn owner_id(&self) -> &str {
        &self.0.owner_id
    }

    /// Returns the current immutable authoring head.
    fn head_revision_id(&self) -> &str {
        &self.0.head_revision_id
    }

    /// Returns the revision currently published to runtime discovery.
    fn published_revision_id(&self) -> Option<&str> {
        self.0.published_revision_id.as_deref()
    }

    /// Returns the current validated pipeline definition.
    fn definition(&self) -> DynamicJson {
        DynamicJson(self.0.definition.clone())
    }

    /// Returns the author of the current head revision.
    fn author_id(&self) -> &str {
        &self.0.author_id
    }

    /// Returns the provenance message for the current head revision.
    fn message(&self) -> &str {
        &self.0.message
    }

    /// Returns creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }

    /// Returns the current ref update time in Unix milliseconds.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }
}

pub struct GqlUser(IamUser);

#[graphql_object(name = "User", context = AppContext)]
impl GqlUser {
    /// Display attributes never participate in authentication or account
    /// linking.
    fn display_name(&self) -> Option<&str> {
        self.0.display_name.as_deref()
    }

    /// Returns the provider's display email without granting identity
    /// authority.
    fn email(&self) -> Option<&str> {
        self.0.email.as_deref()
    }

    /// Returns the stable tenant user identifier.
    fn user_id(&self) -> &str {
        &self.0.user_id
    }

    /// Reports whether identity verification currently accepts the user.
    fn is_active(&self) -> bool {
        self.0.is_active
    }

    /// Returns creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }

    /// Returns the most recent administrative update time.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }
}

pub struct GqlRole(IamRole);

#[derive(juniper::GraphQLObject)]
#[graphql(name = "UserPage", context = AppContext)]
pub struct GqlUserPage {
    pub items: Vec<GqlUser>,
    pub after: Option<String>,
}

pub struct GqlRolePage {
    items: Vec<GqlRole>,
    after: Option<String>,
}

#[graphql_object(name = "RolePage", context = AppContext)]
impl GqlRolePage {
    fn items(&self) -> &[GqlRole] {
        &self.items
    }

    fn after(&self) -> Option<&str> {
        self.after.as_deref()
    }

    /// Resolve only memberships requested for these visible roles, without
    /// listing the tenant.
    async fn assigned_roles(
        &self,
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
    ) -> FieldResult<Vec<String>> {
        let assigned = ctx
            .iam_admin
            .assigned_roles(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
            )
            .await
            .map_err(administration_error)?;
        Ok(self
            .items
            .iter()
            .filter(|role| assigned.contains(&role.0.role_name))
            .map(|role| role.0.role_name.clone())
            .collect())
    }
}

#[graphql_object(name = "Role", context = AppContext)]
impl GqlRole {
    /// Exposes presentation metadata while role_name remains the stable identity.
    fn display_name(&self) -> Option<&str> {
        self.0.display_name.as_deref()
    }
    /// Returns the stable tenant role name.
    fn role_name(&self) -> &str {
        &self.0.role_name
    }

    /// Returns creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }

    /// Returns the most recent administrative update time.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }
}

pub struct GqlRoleAssignment(RoleAssignment);

#[graphql_object(name = "RoleAssignment", context = AppContext)]
impl GqlRoleAssignment {
    /// Returns the assigned tenant user identifier.
    fn user_id(&self) -> &str {
        &self.0.user_id
    }

    /// Returns the assigned tenant role name.
    fn role_name(&self) -> &str {
        &self.0.role_name
    }

    /// Returns assignment creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }
}

#[derive(Debug, Clone, Copy, juniper::GraphQLEnum)]
#[graphql(name = "AuditOutcome")]
pub enum GqlAuditOutcome {
    Attempted,
    Succeeded,
    Failed,
    Denied,
}

impl From<GqlAuditOutcome> for AuditOutcome {
    /// Converts a public filter value into the canonical audit outcome.
    fn from(value: GqlAuditOutcome) -> Self {
        match value {
            GqlAuditOutcome::Attempted => Self::Attempted,
            GqlAuditOutcome::Succeeded => Self::Succeeded,
            GqlAuditOutcome::Failed => Self::Failed,
            GqlAuditOutcome::Denied => Self::Denied,
        }
    }
}

impl From<AuditOutcome> for GqlAuditOutcome {
    /// Converts a persisted outcome into the public GraphQL enum.
    fn from(value: AuditOutcome) -> Self {
        match value {
            AuditOutcome::Attempted => Self::Attempted,
            AuditOutcome::Succeeded => Self::Succeeded,
            AuditOutcome::Failed => Self::Failed,
            AuditOutcome::Denied => Self::Denied,
        }
    }
}

#[derive(juniper::GraphQLInputObject)]
#[graphql(name = "AuditEventFilter")]
pub struct GqlAuditEventFilter {
    pub action: Option<String>,
    pub resource_type: Option<String>,
    pub resource_id: Option<String>,
    pub outcome: Option<GqlAuditOutcome>,
}

impl From<GqlAuditEventFilter> for AuditFilter {
    /// Converts optional public filters without widening their semantics.
    fn from(value: GqlAuditEventFilter) -> Self {
        Self {
            action: value.action,
            resource_type: value.resource_type,
            resource_id: value.resource_id,
            outcome: value.outcome.map(AuditOutcome::from),
        }
    }
}

pub struct GqlAuditEvent(AuditEvent);

#[graphql_object(name = "AuditEvent", context = AppContext)]
impl GqlAuditEvent {
    /// Returns the immutable audit event identifier.
    fn audit_id(&self) -> &str {
        &self.0.audit_id
    }

    /// Returns the identifier shared by attempt and terminal outcomes.
    fn operation_id(&self) -> &str {
        &self.0.operation_id
    }

    /// Returns the authenticated actor category.
    fn actor_type(&self) -> &str {
        &self.0.actor_type
    }

    /// Returns the authenticated actor identifier.
    fn actor_id(&self) -> &str {
        &self.0.actor_id
    }

    /// Returns the canonical sensitive action name.
    fn action(&self) -> &str {
        &self.0.action
    }

    /// Returns the affected resource category.
    fn resource_type(&self) -> &str {
        &self.0.resource_type
    }

    /// Returns the affected tenant-local resource identifier.
    fn resource_id(&self) -> &str {
        &self.0.resource_id
    }

    /// Returns the attempted or terminal operation outcome.
    fn outcome(&self) -> GqlAuditOutcome {
        self.0.outcome.into()
    }

    /// Returns a bounded failure classification without raw errors.
    fn failure_kind(&self) -> Option<&str> {
        self.0.failure_kind.as_deref()
    }

    /// Returns the request correlation identifier.
    fn request_id(&self) -> &str {
        &self.0.request_id
    }

    /// Returns the distributed trace identifier when available.
    fn trace_id(&self) -> Option<&str> {
        self.0.trace_id.as_deref()
    }

    /// Returns the immutable revision identifier when applicable.
    fn revision_id(&self) -> Option<&str> {
        self.0.revision_id.as_deref()
    }

    /// Returns the immutable publication identifier when applicable.
    fn publication_id(&self) -> Option<&str> {
        self.0.publication_id.as_deref()
    }

    /// Returns bounded, non-sensitive action metadata.
    fn details(&self) -> DynamicJson {
        DynamicJson(self.0.details.clone())
    }

    /// Returns occurrence time in Unix milliseconds.
    fn occurred_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.occurred_at_ms)
    }
}

pub struct GqlOntologyPublication(OntologyPublication);

#[graphql_object(name = "OntologyPublication", context = AppContext)]
impl GqlOntologyPublication {
    /// Returns the immutable publication identifier.
    fn publication_id(&self) -> &str {
        &self.0.publication_id
    }

    /// Returns the immutable materialized revision identifier.
    fn revision_id(&self) -> &str {
        &self.0.revision_id
    }

    /// Returns the authoritative publication lifecycle state.
    fn lifecycle(&self) -> &str {
        &self.0.lifecycle
    }

    /// Returns publication metadata supplied by the authoring domain.
    fn metadata(&self) -> DynamicJson {
        DynamicJson(self.0.metadata.clone())
    }

    /// Returns the canonical base ontology version.
    fn base_version(&self) -> &str {
        &self.0.base_version
    }

    /// Returns the canonical base artifact content hash.
    fn base_hash(&self) -> &str {
        &self.0.base_hash
    }

    /// Returns the tenant-effective materialization hash.
    fn effective_hash(&self) -> &str {
        &self.0.effective_hash
    }

    /// Returns the revision author recorded by the Ontology service.
    fn author(&self) -> &str {
        &self.0.author
    }

    /// Returns the immutable revision provenance message.
    fn message(&self) -> &str {
        &self.0.message
    }

    /// Returns publication time in Unix milliseconds.
    fn published_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.published_at_ms)
    }

    /// Returns retirement time in Unix milliseconds when retired.
    fn retired_at_ms(&self) -> Option<f64> {
        self.0.retired_at_ms.map(i64_ms_to_f64)
    }
}

pub struct GqlOntology {
    ontology_id: String,
    display_name: String,
    production_publication: Option<GqlOntologyPublication>,
}

impl From<OntologyCatalogEntry> for GqlOntology {
    /// Maps the authoritative catalog projection into GraphQL.
    fn from(value: OntologyCatalogEntry) -> Self {
        Self {
            ontology_id: value.ontology_id,
            display_name: value.display_name,
            production_publication: value
                .production_publication
                .map(GqlOntologyPublication),
        }
    }
}

#[graphql_object(name = "Ontology", context = AppContext)]
impl GqlOntology {
    /// Returns the tenant-local ontology identifier.
    fn ontology_id(&self) -> &str {
        &self.ontology_id
    }

    /// Returns the current catalog display name.
    fn display_name(&self) -> &str {
        &self.display_name
    }

    /// Returns the sole production publication when one exists.
    fn production_publication(&self) -> Option<&GqlOntologyPublication> {
        self.production_publication.as_ref()
    }
}

pub struct GqlPipelineOntologyBinding(PipelineOntologyBinding);

#[graphql_object(name = "PipelineOntologyBinding", context = AppContext)]
impl GqlPipelineOntologyBinding {
    /// Returns the bound pipeline identifier.
    fn pipeline_id(&self) -> &str {
        &self.0.pipeline_id
    }

    /// Returns the bound pipeline block identifier.
    fn block_id(&self) -> &str {
        &self.0.block_id
    }

    /// Returns the production ontology identifier.
    fn ontology_id(&self) -> &str {
        &self.0.ontology_id
    }

    /// Returns explicitly selected semantic resource identifiers.
    fn resource_ids(&self) -> DynamicJson {
        DynamicJson(self.0.resource_ids.clone())
    }

    /// Returns explicitly selected semantic resource kinds.
    fn resource_kinds(&self) -> DynamicJson {
        DynamicJson(self.0.resource_kinds.clone())
    }

    /// Reports whether dependent semantic resources are included.
    fn include_dependencies(&self) -> bool {
        self.0.include_dependencies
    }

    /// Returns binding metadata from the Ontology service.
    fn metadata(&self) -> DynamicJson {
        DynamicJson(self.0.metadata.clone())
    }

    /// Returns the binding update time in Unix milliseconds.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }
}

pub struct GqlPipelineExecution(PipelineExecution);

#[graphql_object(name = "PipelineExecution", context = AppContext)]
impl GqlPipelineExecution {
    /// Returns the durable idempotency key.
    fn idempotency_key(&self) -> &str {
        &self.0.idempotency_key
    }

    /// Returns the originating pipeline command identifier.
    fn command_id(&self) -> &str {
        &self.0.command_id
    }

    /// Returns the cross-service correlation identifier.
    fn correlation_id(&self) -> &str {
        &self.0.correlation_id
    }

    /// Returns the executed pipeline identifier.
    fn pipeline_id(&self) -> &str {
        &self.0.pipeline_id
    }

    /// Returns the executed pipeline step.
    fn step(&self) -> &str {
        &self.0.step
    }

    /// Returns the durable execution status.
    fn status(&self) -> &str {
        &self.0.status
    }

    /// Returns the current execution attempt number.
    fn attempt(&self) -> i32 {
        self.0.attempt
    }

    /// Returns lease expiry in Unix milliseconds.
    fn lease_expires_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.lease_expires_at_ms)
    }

    /// Returns the terminal execution result when available.
    fn result(&self) -> Option<DynamicJson> {
        self.0.result.clone().map(DynamicJson)
    }

    /// Returns the bounded terminal error when available.
    fn error(&self) -> Option<&str> {
        self.0.error.as_deref()
    }

    /// Returns execution creation time in Unix milliseconds.
    fn created_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.created_at_ms)
    }

    /// Returns the most recent execution update time.
    fn updated_at_ms(&self) -> f64 {
        i64_ms_to_f64(self.0.updated_at_ms)
    }
}

#[graphql_object(name = "GraphSubgraph", context = AppContext)]
impl GqlGraphSubgraph {
    /// Returns permission-filtered graph nodes.
    fn nodes(&self) -> &[GqlGraphNode] {
        &self.nodes
    }

    /// Returns edges whose endpoints survived authorization filtering.
    fn edges(&self) -> &[GqlGraphEdge] {
        &self.edges
    }
}

/// Converts internal millisecond timestamps to Juniper's supported scalar.
fn i64_ms_to_f64(ms: i64) -> f64 {
    ms as f64
}

/// Maps one authorized domain search result without changing its identity.
fn global_hit_to_gql(hit: GlobalSearchHit) -> GqlGlobalSearchHit {
    match hit {
        GlobalSearchHit::EntityState {
            entity_id, state, ..
        } => GqlGlobalSearchHit {
            kind: "entity_state".to_string(),
            entity_id: Some(entity_id),
            event_id: None,
            event_type: None,
            modality: None,
            created_at_ms: None,
            event_time_ms: None,
            score: None,
            payload: state,
        },
        GlobalSearchHit::Event {
            event_id,
            event_type,
            event_time_ms,
            properties,
        } => GqlGlobalSearchHit {
            kind: "event".to_string(),
            entity_id: None,
            event_id: Some(event_id),
            event_type: Some(event_type),
            modality: None,
            created_at_ms: None,
            event_time_ms: Some(i64_ms_to_f64(event_time_ms)),
            score: None,
            payload: properties,
        },
        GlobalSearchHit::Embedding {
            entity_id,
            modality,
            created_at_ms,
            metadata,
            score,
        } => GqlGlobalSearchHit {
            kind: "embedding".to_string(),
            entity_id: Some(entity_id),
            event_id: None,
            event_type: None,
            modality: Some(modality),
            created_at_ms: Some(i64_ms_to_f64(created_at_ms)),
            event_time_ms: None,
            score: Some(score as f64),
            payload: metadata,
        },
    }
}

/// Returns actionable public errors without exposing storage or provider
/// details.
fn administration_error(error: anyhow::Error) -> FieldError {
    let messages = error.chain().map(ToString::to_string).collect::<Vec<_>>();
    let has =
        |part: &str| messages.iter().any(|message| message.contains(part));
    let (code, message) = if has("own account") {
        (
            "SELF_SUSPENSION",
            "You cannot suspend or delete your own account. Ask another administrator to make this change.",
        )
    } else if has("active administrator must") {
        (
            "LAST_ADMINISTRATOR",
            "An active administrator must remain in the organization.",
        )
    } else if has("Authorization denied")
        || has("not a tenant admin")
        || has("access is denied")
    {
        (
            "FORBIDDEN",
            "You do not have permission to make this change in this organization.",
        )
    } else if has("Permission import disabled")
        || has("Permission import is disabled")
    {
        (
            "PERMISSION_IMPORT_DISABLED",
            "Enable automatic permission import before applying these rules.",
        )
    } else if messages.iter().any(|message| {
        message.starts_with("User '")
            && (message.ends_with("is disabled")
                || message.ends_with("not found"))
    }) {
        (
            "UNAUTHENTICATED",
            "Your account is unavailable. Sign in again or contact an administrator.",
        )
    } else if has("managed by your identity provider") {
        (
            "MANAGED_BY_PROVIDER",
            "This role is managed by your identity provider. Change its group mapping before removing it here.",
        )
    } else if has("limit of 256 role names") {
        (
            "LIMIT_EXCEEDED",
            "This organization has reached its limit of 256 role names, including archived roles. Contact support to review its capacity.",
        )
    } else if has("limit of 128 access policies") {
        (
            "LIMIT_EXCEEDED",
            "This organization has reached its limit of 128 access policies, including inactive policies. Update an existing policy instead.",
        )
    } else if has("8192 bytes") || has("policy content budget") {
        (
            "LIMIT_EXCEEDED",
            "Keep each access policy within 8 KB and the organization's combined active policies within 1 MB.",
        )
    } else if has("at most 16 levels") {
        (
            "LIMIT_EXCEEDED",
            "Keep the role hierarchy within 16 levels so access checks remain fast and reliable.",
        )
    } else if has("hierarchy connections before deleting") {
        (
            "CONFLICT",
            "Remove this role's parent and child connections before deleting it.",
        )
    } else if has("domain") && (has("verified") || has("ownership")) {
        (
            "DOMAIN_NOT_VERIFIED",
            "Verify your company email domain before connecting your identity provider.",
        )
    } else if has("cycle") || has("itself") {
        (
            "INVALID_INPUT",
            "This role hierarchy would contain a cycle. Choose a different parent role.",
        )
    } else if has("Remove the current parent")
        || has("Remove the existing parent")
    {
        (
            "CONFLICT",
            "Remove the current parent role before choosing another one.",
        )
    } else if has("Invalid organization name") {
        (
            "INVALID_INPUT",
            "Enter an organization name between 1 and 128 characters, without control characters.",
        )
    } else if has("Invalid local identifier") {
        (
            "INVALID_INPUT",
            "Use 1 to 64 letters, numbers, hyphens or underscores for this identifier.",
        )
    } else if has("OIDC issuer requires HTTPS") {
        (
            "INVALID_INPUT",
            "Use the HTTPS issuer address provided by your identity provider.",
        )
    } else if has("Supply both OIDC credentials")
        || has("Invalid OIDC credentials")
    {
        (
            "INVALID_INPUT",
            "Enter both the client ID and client secret, or leave both empty to use discovery.",
        )
    } else if has("Invalid SAML metadata") {
        (
            "INVALID_INPUT",
            "Enter the HTTPS metadata URL or valid SAML metadata XML from your identity provider.",
        )
    } else if has("Invalid email domain")
        || has("Invalid domain label")
        || has("Domain must be canonical ASCII")
        || has("IP address is not a domain")
    {
        (
            "INVALID_INPUT",
            "Enter a company email domain, such as example.com.",
        )
    } else if has("Both roles must exist") {
        (
            "INVALID_INPUT",
            "Create both roles in this organization before connecting them.",
        )
    } else if has("Invalid Cedar policy") {
        (
            "INVALID_INPUT",
            "The policy could not be parsed as Cedar. Check its syntax before saving.",
        )
    } else if has("Invalid")
        || has("invalid")
        || has("must")
        || has("Choose a")
        || has("unsupported")
        || has("exceeds")
    {
        (
            "INVALID_INPUT",
            "Check the values entered. One or more fields are invalid or exceed the allowed limits.",
        )
    } else if has("unavailable") || has("could not") {
        (
            "PROVIDER_UNAVAILABLE",
            "The authentication or permissions service is temporarily unavailable. Try again shortly.",
        )
    } else {
        (
            "INTERNAL_ERROR",
            "The change could not be saved. Try again; contact support with the request identifier if it continues.",
        )
    };
    tracing::warn!(
        event.name = "administration.operation.rejected",
        error.code = code,
        "administration operation rejected"
    );
    FieldError::new(message, juniper::graphql_value!({"code":code}))
}

pub struct AdministrationQuery;

#[graphql_object(context = AppContext)]
impl AdministrationQuery {
    /// Lists provider-managed group mappings without exposing provider
    /// secrets.
    async fn role_sync_configurations(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<Vec<admin_types::SyncConfiguration>> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            administration_error(anyhow::anyhow!(
                "SSO management is unavailable"
            ))
        })?;
        let value = service
            .role_sync_configurations(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
            )
            .await
            .map_err(administration_error)?;
        Ok(value.into_iter().map(Into::into).collect())
    }

    /// Lists immutable audit history for authorized tenant administrators.
    async fn audit_events(
        #[graphql(context)] ctx: &AppContext,
        filter: Option<GqlAuditEventFilter>,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlAuditEvent>> {
        let filter = filter.map(AuditFilter::from).unwrap_or_default();
        let events = ctx
            .control_plane
            .audit_events(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &filter,
                control_plane_limit(limit),
            )
            .await
            .map_err(administration_error)?;
        Ok(events.into_iter().map(GqlAuditEvent).collect())
    }

    /// Returns only this administrator's organization configuration, without
    /// secrets.
    async fn sso_settings(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<admin_types::Settings> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        let value = service
            .settings(&ctx.tenant_id, &ctx.user_id, &ctx.authz_context)
            .await
            .map_err(administration_error)?;
        Ok(value.into())
    }

    /// Lists tenant users for authorized tenant administrators.
    async fn users(
        #[graphql(context)] ctx: &AppContext,
        search: Option<String>,
        limit: Option<i32>,
        after: Option<String>,
    ) -> FieldResult<GqlUserPage> {
        let users = ctx
            .iam_admin
            .users_page(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                search.as_deref().unwrap_or_default(),
                limit.unwrap_or(20).clamp(1, 100) as usize,
                after.as_deref(),
            )
            .await
            .map_err(administration_error)?;
        Ok(GqlUserPage {
            items: users.items.into_iter().map(GqlUser).collect(),
            after: users.after,
        })
    }

    /// Lists tenant roles for authorized tenant administrators.
    async fn roles(
        #[graphql(context)] ctx: &AppContext,
        search: Option<String>,
        limit: Option<i32>,
        after: Option<String>,
    ) -> FieldResult<GqlRolePage> {
        let roles = ctx
            .iam_admin
            .roles_page(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                search.as_deref().unwrap_or_default(),
                limit.unwrap_or(20).clamp(1, 100) as usize,
                after.as_deref(),
            )
            .await
            .map_err(administration_error)?;
        Ok(GqlRolePage {
            items: roles.items.into_iter().map(GqlRole).collect(),
            after: roles.after,
        })
    }

    /// Lists durable role memberships for authorized tenant administrators.
    async fn role_assignments(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlRoleAssignment>> {
        let assignments = ctx
            .iam_admin
            .role_assignments(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await
            .map_err(administration_error)?;
        Ok(assignments.into_iter().map(GqlRoleAssignment).collect())
    }

    /// Returns a bounded graph after authorizing both relationship endpoints.
    async fn access_graph(
        #[graphql(context)] ctx: &AppContext,
        search: Option<String>,
        limit: Option<i32>,
    ) -> FieldResult<admin_types::AccessGraph> {
        let value = ctx
            .iam_admin
            .access_graph(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                search.as_deref().unwrap_or_default(),
                control_plane_limit(limit),
            )
            .await
            .map_err(administration_error)?;
        serde_json::from_value(value).map_err(|_| {
            administration_error(anyhow::anyhow!(
                "Invalid access graph projection"
            ))
        })
    }

    /// Returns the organization's policy source and activation state.
    async fn cedar_policies(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<Vec<admin_types::Policy>> {
        let value = ctx
            .iam_admin
            .cedar_policies(&ctx.tenant_id, &ctx.user_id, &ctx.authz_context)
            .await
            .map_err(administration_error)?;
        Ok(value.into_iter().map(Into::into).collect())
    }
}

pub struct Query;

#[graphql_object(context = AppContext)]
impl Query {
    /// Describes trusted installed blocks without granting pipeline execution.
    async fn pipeline_capabilities(
        #[graphql(context)] ctx: &AppContext,
        kind: Option<capabilities::Kind>,
        plugin_id: Option<String>,
    ) -> FieldResult<capabilities::Catalog> {
        ctx.verify_authentication().map_err(|_| capabilities::discovery_error(crate::application::usecases::capabilities::DiscoveryError::Forbidden))?;
        let catalog = ctx
            .pipelines
            .capabilities(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(capabilities::discovery_error)?;
        capabilities::project(
            &catalog,
            crate::domain::capabilities::CapabilityTarget::Pipeline,
            kind,
            plugin_id.as_deref(),
        )
    }

    /// Reflects the deployed Scribe registry, excluding private MCP endpoints.
    async fn scribe_capabilities(
        #[graphql(context)] ctx: &AppContext,
        kind: Option<capabilities::Kind>,
        plugin_id: Option<String>,
    ) -> FieldResult<capabilities::Catalog> {
        ctx.verify_authentication().map_err(|_| capabilities::discovery_error(crate::application::usecases::capabilities::DiscoveryError::Forbidden))?;
        let catalog = ctx
            .conversations
            .capabilities(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(capabilities::discovery_error)?;
        capabilities::project(
            &catalog,
            crate::domain::capabilities::CapabilityTarget::Scribe,
            kind,
            plugin_id.as_deref(),
        )
    }

    /// Groups authenticated organization administration in one namespace.
    fn administration() -> AdministrationQuery {
        AdministrationQuery
    }

    /// Searches entity states by name and filters every result by permission.
    async fn search_entities(
        #[graphql(context)] ctx: &AppContext,
        query: String,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlSearchHit>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let lim = limit.unwrap_or(20).clamp(1, 50) as usize;

        let hits = ctx
            .explore
            .search_entities_by_name(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &query,
                lim,
            )
            .await?;

        let mut out = Vec::with_capacity(hits.len());
        for h in hits {
            out.push(GqlSearchHit {
                entity_id: h.entity_id,
                metadata: h.metadata,
            });
        }
        Ok(out)
    }

    /// Continues a bounded search while rechecking access to every source row.
    async fn global_search_page(
        #[graphql(context)] ctx: &AppContext,
        query: String,
        limit: Option<i32>,
        after: Option<String>,
        locale: Option<String>,
    ) -> FieldResult<GqlSearchPage> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await?;
        let navigation = if after.is_none() && !query.contains(':') {
            let locale = if locale
                .as_deref()
                .is_some_and(|locale| locale.starts_with("fr"))
            {
                "fr"
            } else {
                "en"
            };
            ctx.search
                .navigation(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &query,
                )
                .await?
                .into_iter()
                .map(|(entry, score)| GqlNavigationHit {
                    id: entry.id.clone(),
                    kind: if entry.kind
                        == crate::domain::navigation::NavigationKind::Action
                    {
                        "action"
                    } else {
                        "navigation"
                    }
                    .into(),
                    href: entry.href.clone(),
                    label: entry
                        .labels
                        .get(locale)
                        .cloned()
                        .unwrap_or_default(),
                    description: entry
                        .descriptions
                        .get(locale)
                        .cloned()
                        .unwrap_or_default(),
                    score,
                })
                .collect()
        } else {
            Vec::new()
        };
        let page = ctx
            .search
            .global_search_page(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &query,
                limit.unwrap_or(20).clamp(1, 50) as usize,
                after.as_deref(),
            )
            .await?;
        Ok(GqlSearchPage {
            items: page.items.into_iter().map(global_hit_to_gql).collect(),
            after: page.after,
            navigation,
        })
    }

    /// Selectable structured results use the same live authorization as search
    /// tools.
    async fn structured_search_page(
        #[graphql(context)] ctx: &AppContext,
        input: GqlStructuredSearchInput,
        after: Option<String>,
    ) -> FieldResult<GqlSearchPage> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await?;
        let page = ctx
            .search
            .structured_search_page(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                StructuredSearchQuery {
                    text: input.text.as_deref(),
                    entity_id: input.entity_id.as_deref(),
                    event_type: input.event_type.as_deref(),
                    modality: input.modality.as_deref(),
                },
                input.limit.unwrap_or(20).clamp(1, 50) as usize,
                after.as_deref(),
            )
            .await?;
        Ok(GqlSearchPage {
            items: page.items.into_iter().map(global_hit_to_gql).collect(),
            after: page.after,
            navigation: Vec::new(),
        })
    }

    /// Global search (text-only). Supports token syntax:  `entity_id:...
    /// event:... modality:... <free text>`.
    async fn global_search(
        #[graphql(context)] ctx: &AppContext,
        query: String,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlGlobalSearchHit>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let lim = limit.unwrap_or(20).clamp(1, 50) as usize;

        let hits = ctx
            .search
            .global_search(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &query,
                lim,
            )
            .await
            .map_err(FieldError::from)?;

        Ok(hits.into_iter().map(global_hit_to_gql).collect())
    }

    /// Searches tenant data with explicit semantic filters and per-hit
    /// authorization.
    async fn structured_search(
        #[graphql(context)] ctx: &AppContext,
        input: GqlStructuredSearchInput,
    ) -> FieldResult<Vec<GqlGlobalSearchHit>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;
        let limit = input.limit.unwrap_or(20).clamp(1, 50) as usize;
        let query = StructuredSearchQuery {
            text: input.text.as_deref(),
            entity_id: input.entity_id.as_deref(),
            event_type: input.event_type.as_deref(),
            modality: input.modality.as_deref(),
        };
        let hits = ctx
            .search
            .structured_search(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                query,
                limit,
            )
            .await?;
        Ok(hits.into_iter().map(global_hit_to_gql).collect())
    }

    /// Explicit event search exposes selectable identifiers and provenance.
    async fn search_events(
        #[graphql(context)] ctx: &AppContext,
        event_type: Option<String>,
        text: Option<String>,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlGlobalSearchHit>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let lim = limit.unwrap_or(20).clamp(1, 50) as usize;

        let rows = ctx
            .search
            .search_events_explicit(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                event_type.as_deref(),
                text.as_deref(),
                lim,
            )
            .await
            .map_err(FieldError::from)?;

        Ok(rows
            .into_iter()
            .map(|row| {
                global_hit_to_gql(GlobalSearchHit::Event {
                    event_id: row.event_id,
                    event_type: row.event_type,
                    event_time_ms: row.event_time_ms,
                    properties: row.properties,
                })
            })
            .collect())
    }

    /// Explicit embedding search exposes selectable metadata and scores.
    async fn search_embeddings(
        #[graphql(context)] ctx: &AppContext,
        query_text: String,
        modality: Option<String>,
        k: Option<i32>,
    ) -> FieldResult<Vec<GqlGlobalSearchHit>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let kk = k.unwrap_or(10).clamp(1, 50) as usize;

        let rows = ctx
            .search
            .search_embeddings_explicit(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &query_text,
                modality.as_deref(),
                kk,
            )
            .await
            .map_err(FieldError::from)?;

        Ok(rows
            .into_iter()
            .map(|row| {
                global_hit_to_gql(GlobalSearchHit::Embedding {
                    entity_id: row.entity_id,
                    modality: row.modality,
                    created_at_ms: row.created_at_ms,
                    metadata: row.metadata,
                    score: row.score,
                })
            })
            .collect())
    }

    /// Fetches permission-filtered k-hop relations for an entity.
    async fn entity_relations(
        #[graphql(context)] ctx: &AppContext,
        entity_id: String,
        depth: Option<i32>,
        limit: Option<i32>,
    ) -> FieldResult<GqlGraphSubgraph> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let d = depth.unwrap_or(1).clamp(1, 3) as u8;
        let lim = limit.unwrap_or(30).clamp(1, 50) as usize;

        let g = ctx
            .explore
            .entity_relations_filtered(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &entity_id,
                d,
                lim,
            )
            .await?;

        let mut nodes = Vec::with_capacity(g.nodes.len());
        for n in g.nodes {
            nodes.push(GqlGraphNode {
                id: n.id,
                label: n.kind.as_str().to_owned(),
                ontology_ref: n.ontology_ref.map(GqlOntologyReference),
                properties: n.properties,
            });
        }

        let mut edges = Vec::with_capacity(g.edges.len());
        for e in g.edges {
            edges.push(GqlGraphEdge {
                from_id: e.from_id,
                to_id: e.to_id,
                label: e.label,
                properties: e.properties,
            });
        }

        Ok(GqlGraphSubgraph { nodes, edges })
    }

    /// Lists authorized ontology catalog entries and production publications.
    async fn ontologies(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlOntology>> {
        let ontologies = ctx
            .control_plane
            .ontologies(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await?;
        Ok(ontologies.into_iter().map(GqlOntology::from).collect())
    }

    /// Lists authorized pipeline block bindings to published ontologies.
    async fn ontology_bindings(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: Option<String>,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlPipelineOntologyBinding>> {
        let bindings = ctx
            .control_plane
            .ontology_bindings(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                pipeline_id.as_deref(),
                control_plane_limit(limit),
            )
            .await?;
        Ok(bindings
            .into_iter()
            .map(GqlPipelineOntologyBinding)
            .collect())
    }

    /// Lists authorized durable pipeline step execution history.
    async fn pipeline_executions(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: Option<String>,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlPipelineExecution>> {
        let executions = ctx
            .control_plane
            .pipeline_executions(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                pipeline_id.as_deref(),
                control_plane_limit(limit),
            )
            .await?;
        Ok(executions.into_iter().map(GqlPipelineExecution).collect())
    }

    async fn pengolo_identity(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<pengolo::Identity> {
        pengolo_result(ctx.pengolo.identity(&pengolo_actor(ctx)?).await)
    }

    async fn pengolo_models(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<GqlScribeModelCatalog> {
        Ok(ctx.pengolo.models(&pengolo_actor(ctx)?).await?.into())
    }

    async fn pengolo_openai(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<pengolo::Openai> {
        pengolo_result(ctx.pengolo.openai_account(&pengolo_actor(ctx)?).await)
    }

    async fn pengolo_projects(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<Vec<pengolo::Project>> {
        pengolo_result(ctx.pengolo.projects(&pengolo_actor(ctx)?).await)
    }

    async fn pengolo_agents(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<Vec<pengolo::Agent>> {
        pengolo_result(ctx.pengolo.agents(&pengolo_actor(ctx)?).await)
    }

    async fn pengolo_chats(
        #[graphql(context)] ctx: &AppContext,
        after: Option<String>,
        project_id: Option<String>,
    ) -> FieldResult<pengolo::ChatPage> {
        pengolo_result(
            ctx.pengolo
                .chats(
                    &pengolo_actor(ctx)?,
                    after.as_deref(),
                    project_id.as_deref(),
                )
                .await,
        )
    }

    async fn pengolo_chat(
        #[graphql(context)] ctx: &AppContext,
        id: String,
        temporary: Option<bool>,
    ) -> FieldResult<Option<pengolo::Chat>> {
        pengolo_result(
            ctx.pengolo
                .chat(&pengolo_actor(ctx)?, &id, temporary.unwrap_or(false))
                .await,
        )
        .map(Some)
    }

    async fn pengolo_library(
        #[graphql(context)] ctx: &AppContext,
        project_id: Option<String>,
        search: Option<String>,
        after: Option<String>,
    ) -> FieldResult<pengolo::FilePage> {
        pengolo_result(
            ctx.pengolo
                .library(
                    &pengolo_actor(ctx)?,
                    project_id.as_deref(),
                    search.as_deref().unwrap_or(""),
                    after.as_deref(),
                )
                .await,
        )
    }

    async fn document_search(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::DocumentSearchInput,
    ) -> FieldResult<pengolo::DocumentSearchPage> {
        let mut value = pengolo_input(input)?;
        if let Some(fields) = value.as_object_mut() {
            fields.retain(|_, value| !value.is_null());
        }
        pengolo_result(
            ctx.pengolo
                .document_search(&pengolo_actor(ctx)?, value)
                .await,
        )
    }

    async fn pengolo_events(
        #[graphql(context)] ctx: &AppContext,
        generation_id: String,
        after: i32,
        temporary: Option<bool>,
    ) -> FieldResult<pengolo::Replay> {
        pengolo_result(
            ctx.pengolo
                .events(
                    &pengolo_actor(ctx)?,
                    &generation_id,
                    after,
                    temporary.unwrap_or(false),
                )
                .await,
        )
    }

    /// Exposes model choices through the authenticated public API.
    async fn scribe_models(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<GqlScribeModelCatalog> {
        ctx.verify_authentication()?;
        Ok(ctx
            .conversations
            .models(&ctx.tenant_id, &ctx.user_id)
            .await?
            .into())
    }

    /// Lists only conversations visible to the authenticated principal.
    async fn conversations(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlConversation>> {
        let conversations = ctx
            .conversations
            .conversations(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await?;
        Ok(conversations.into_iter().map(GqlConversation).collect())
    }

    /// Loads one authorized conversation and its current message projection.
    async fn generation_events(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        generation_id: String,
        after: i32,
    ) -> FieldResult<Vec<GqlGenerationEvent>> {
        Ok(ctx
            .conversations
            .generation_events(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &conversation_id,
                &generation_id,
                after,
            )
            .await?
            .into_iter()
            .map(|event| GqlGenerationEvent {
                sequence: event.sequence,
                kind: event.kind,
                content: event.content,
            })
            .collect())
    }

    /// Loads one authorized conversation and its current message projection.
    async fn conversation(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        include_deleted_messages: Option<bool>,
    ) -> FieldResult<Option<GqlConversation>> {
        Ok(ctx
            .conversations
            .conversation(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &conversation_id,
                include_deleted_messages.unwrap_or(false),
            )
            .await?
            .map(GqlConversation))
    }

    /// Reads an authored revision after checking current view permission.
    async fn pipeline_definition(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: String,
        revision_id: Option<String>,
    ) -> FieldResult<Option<GqlPipelineDefinition>> {
        ctx.verify_authentication()?;
        ctx.pipelines
            .read(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &pipeline_id,
                revision_id.as_deref(),
            )
            .await
            .map(|item| item.map(GqlPipelineDefinition))
            .map_err(pipeline_error)
    }

    /// Lists only readable, executable, published transfer targets.
    async fn pipeline_link_targets(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlPipelineDefinition>> {
        ctx.verify_authentication()?;
        ctx.pipelines
            .link_targets(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await
            .map(|items| {
                items.into_iter().map(GqlPipelineDefinition).collect()
            })
            .map_err(pipeline_error)
    }

    /// Previews an authorized immutable composition without mutating Registry.
    async fn pipeline_composition(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: String,
        definition: DynamicJson,
    ) -> FieldResult<GqlPipelineComposition> {
        ctx.verify_authentication()?;
        let result = ctx
            .pipelines
            .compose(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &pipeline_id,
                &definition.0,
                false,
            )
            .await
            .map_err(pipeline_error)?;
        Ok(GqlPipelineComposition {
            definition: DynamicJson(result.definition),
            blocks: i32::try_from(result.blocks)
                .map_err(|error| pipeline_error(error.into()))?,
            transfers: i32::try_from(result.transfers)
                .map_err(|error| pipeline_error(error.into()))?,
        })
    }

    /// Lists permission-filtered current pipeline definitions.
    async fn pipeline_definitions(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlPipelineDefinition>> {
        let pipelines = ctx
            .pipelines
            .list(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await?;
        Ok(pipelines.into_iter().map(GqlPipelineDefinition).collect())
    }
}

/// Keeps authorization errors actionable without exposing dependency
/// internals.
fn pipeline_error(error: anyhow::Error) -> FieldError {
    let forbidden = error.downcast_ref::<crate::application::usecases::pipelines::PipelineAccessDenied>().is_some();
    FieldError::new(
        if forbidden {
            "Pipeline access denied"
        } else {
            "Pipeline is unavailable, incompatible, or exceeds composition limits"
        },
        juniper::graphql_value!({"code": if forbidden { "FORBIDDEN" } else { "PIPELINE_COMPOSITION_INVALID" }}),
    )
}

/// Applies the public control-plane pagination boundary consistently.
fn control_plane_limit(limit: Option<i32>) -> usize {
    limit.unwrap_or(50).clamp(1, 100) as usize
}

/// Converts GraphQL attachment inputs into persistence-safe domain values.
fn message_attachments(
    inputs: Option<Vec<GqlMessageAttachmentInput>>,
) -> FieldResult<Vec<MessageAttachment>> {
    inputs
        .unwrap_or_default()
        .into_iter()
        .map(|input| {
            let object_key = input.object_key.trim().to_owned();
            if object_key.is_empty() {
                return Err(FieldError::new(
                    "Attachment object key is required",
                    juniper::Value::Null,
                ));
            }
            Ok(MessageAttachment {
                object_key,
                kind: input.kind.into(),
                file_name: input.file_name,
                content_type: input.content_type,
                size_bytes: input.size_bytes.map(i64::from),
            })
        })
        .collect()
}

/// Parses a lossless optimistic revision supplied by a GraphQL client.
fn parse_expected_revision(value: &str) -> FieldResult<i64> {
    value
        .parse::<i64>()
        .ok()
        .filter(|revision| *revision >= 0)
        .ok_or_else(|| {
            FieldError::new(
                "Expected revision must be a non-negative decimal string",
                juniper::Value::Null,
            )
        })
}

/// Represents the generated S3 direct-upload target package.
#[derive(Debug, Clone)]
pub struct PresignedUpload {
    pub upload_url: String,
    pub staging_key: String,
}

#[graphql_object(context = AppContext)]
impl PresignedUpload {
    /// Returns the short-lived direct upload URL.
    fn upload_url(&self) -> &str {
        &self.upload_url
    }

    /// Returns the owner-scoped staging key required by completion.
    fn staging_key(&self) -> &str {
        &self.staging_key
    }
}

pub struct AdministrationMutation;

#[graphql_object(context = AppContext)]
impl AdministrationMutation {
    /// Saves only explicit, tenant-approved identity-provider role mappings.
    async fn configure_role_sync(
        #[graphql(context)] ctx: &AppContext,
        configuration: admin_types::SyncInput,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            administration_error(anyhow::anyhow!(
                "SSO management is unavailable"
            ))
        })?;
        let input: crate::authentication::FederationConfig =
            configuration.into();
        service
            .configure_role_sync(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &input,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Previews detected public metadata without modifying the tenant's
    /// connection.
    async fn detect_sso_provider(
        #[graphql(context)] ctx: &AppContext,
        domain: String,
        address: String,
    ) -> FieldResult<admin_types::Preview> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        let value = service
            .detect_provider(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &domain,
                &address,
            )
            .await
            .map_err(administration_error)?;
        value.try_into().map_err(administration_error)
    }

    /// Creates a DNS ownership challenge for this organization.
    async fn request_sso_domain(
        #[graphql(context)] ctx: &AppContext,
        domain: String,
    ) -> FieldResult<admin_types::Challenge> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        let value = service
            .request_domain(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &domain,
            )
            .await
            .map_err(administration_error)?;
        Ok(value.into())
    }

    /// Sends write-only provider credentials to the private encrypted broker.
    async fn configure_sso_connection(
        #[graphql(context)] ctx: &AppContext,
        configuration: admin_types::ConnectionConfiguration,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        let input: crate::domain::sso::ConnectionInput = configuration.into();
        service
            .configure(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &input,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Stops discovery while preserving existing account bindings.
    async fn disable_sso_connection(
        #[graphql(context)] ctx: &AppContext,
        domain: String,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        service
            .disable(&ctx.tenant_id, &ctx.user_id, &ctx.authz_context, &domain)
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Associates immutable subjects without merging accounts by email.
    async fn link_external_identity(
        #[graphql(context)] ctx: &AppContext,
        domain: String,
        external_subject: String,
        user_id: String,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        service
            .link_identity(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &domain,
                &external_subject,
                &user_id,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Explicitly enables or disables this organization's permission imports.
    async fn set_permission_import(
        #[graphql(context)] ctx: &AppContext,
        enabled: bool,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        service
            .set_import(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                enabled,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Updates display metadata without changing the immutable tenant
    /// identity.
    async fn update_organization(
        #[graphql(context)] ctx: &AppContext,
        name: String,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        service
            .update_organization(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &name,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Changes the tenant's default-disabled passkey second-factor
    /// requirement.
    async fn set_passkey_mfa(
        #[graphql(context)] ctx: &AppContext,
        enabled: bool,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        service
            .set_passkey_mfa(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                enabled,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Recovers an account by invalidating its credentials and pending proofs.
    async fn reset_user_passkeys(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
    ) -> FieldResult<bool> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        service
            .reset_passkeys(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Imports compatible permissions under the authenticated tenant boundary.
    async fn import_permissions(
        #[graphql(context)] ctx: &AppContext,
        manifest: admin_types::ManifestInput,
    ) -> FieldResult<admin_types::Import> {
        let service = ctx.sso_admin.as_ref().ok_or_else(|| {
            FieldError::from("SSO management is unavailable")
        })?;
        if serde_json::to_vec(&manifest)?.len() > 131_072 {
            return Err(FieldError::from(
                "Permission manifest exceeds 128 KiB",
            ));
        }
        let manifest = manifest.try_into().map_err(administration_error)?;
        let value = service
            .import(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &manifest,
            )
            .await
            .map_err(administration_error)?;
        value.try_into().map_err(administration_error)
    }

    /// Creates a tenant user after administrative authorization.
    async fn create_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        is_active: Option<bool>,
    ) -> FieldResult<bool> {
        let active = is_active.unwrap_or(true);

        ctx.iam_admin
            .create_user(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
                active,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Assigns or revokes one bounded group privilege as tenant administrator.
    async fn set_data_domain_grant(
        #[graphql(context)] ctx: &AppContext,
        group_id: String,
        user_id: String,
        grant: GqlDataDomainGrant,
        enabled: bool,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .set_data_domain_grant(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                DataDomainGrantRequest {
                    group_id: &group_id,
                    subject_user_id: &user_id,
                    grant: grant.into(),
                    enabled,
                },
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Updates only an administrator-controlled directory display label.
    async fn set_directory_display_name(
        #[graphql(context)] ctx: &AppContext,
        subject_type: String,
        subject_id: String,
        display_name: String,
    ) -> FieldResult<bool> {
        use crate::domain::directory_label::DirectorySubject;
        let subject = match subject_type.as_str() {
            "user" => DirectorySubject::User,
            "role" => DirectorySubject::Role,
            _ => {
                return Err(administration_error(anyhow::anyhow!(
                    "Choose a user or role"
                )));
            },
        };
        ctx.iam_admin
            .set_display_name(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                subject,
                &subject_id,
                &display_name,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Activates or disables an existing tenant user.
    async fn update_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        is_active: bool,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .update_user(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
                is_active,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Tombstones a user identifier and removes known assignments.
    async fn delete_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .delete_user(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Creates a tenant role after administrative authorization.
    async fn create_role(
        #[graphql(context)] ctx: &AppContext,
        role_name: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .create_role(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &role_name,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Tombstones a role after deleting every durable assignment.
    async fn delete_role(
        #[graphql(context)] ctx: &AppContext,
        role_name: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .delete_role(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &role_name,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Assigns an active tenant user to a current tenant role.
    async fn assign_role_to_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        role_name: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .assign_role_to_user(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
                &role_name,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Removes one durable tenant user-to-role assignment.
    async fn unassign_role_from_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        role_name: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .unassign_role_from_user(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &user_id,
                &role_name,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Validates and persists a deny-capable tenant Cedar policy.
    async fn set_cedar_policy(
        #[graphql(context)] ctx: &AppContext,
        policy_id: String,
        content: String,
        is_active: Option<bool>,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .set_cedar_policy(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &policy_id,
                &content,
                is_active.unwrap_or(true),
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Changes one role inheritance edge within the authenticated
    /// organization.
    async fn set_role_parent(
        #[graphql(context)] ctx: &AppContext,
        role_name: String,
        parent_role_name: String,
        enabled: bool,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .set_role_parent(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &role_name,
                &parent_role_name,
                enabled,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Grants or revokes one supported permission on an existing resource.
    async fn set_resource_access(
        #[graphql(context)] ctx: &AppContext,
        resource_type: String,
        resource_id: String,
        subject_type: String,
        subject_id: String,
        relation: String,
        enabled: bool,
    ) -> FieldResult<bool> {
        let tuple = crate::application::ports::access_store::AccessTuple {
            resource_type,
            resource_id,
            relation,
            subject_relation: if subject_type == "role" {
                "member".into()
            } else {
                String::new()
            },
            subject_type,
            subject_id,
        };
        ctx.iam_admin
            .set_resource_access(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &tuple,
                enabled,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }

    /// Compiles guided rules on the API so every client shares validation.
    async fn configure_general_access(
        #[graphql(context)] ctx: &AppContext,
        id: String,
        configuration: admin_types::GeneralAccess,
        active: bool,
    ) -> FieldResult<bool> {
        let configuration =
            configuration.try_into().map_err(administration_error)?;
        let content=crate::application::usecases::general_access::build_general_access_policy(&configuration).map_err(administration_error)?;
        ctx.iam_admin
            .set_cedar_policy(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &id,
                &content,
                active,
            )
            .await
            .map_err(administration_error)?;
        Ok(true)
    }
}

pub struct Mutation;

#[graphql_object(context = AppContext)]
impl Mutation {
    /// Submit one administrative write per request because nested GraphQL
    /// fields may execute concurrently.
    fn administration() -> AdministrationMutation {
        AdministrationMutation
    }

    /// Publishes a validated ontology materialization as production.
    async fn publish_ontology(
        #[graphql(context)] ctx: &AppContext,
        ontology_id: String,
        display_name: String,
        revision_id: String,
        metadata: DynamicJson,
    ) -> FieldResult<GqlOntologyPublication> {
        Ok(GqlOntologyPublication(
            ctx.control_plane
                .publish_ontology(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &ontology_id,
                    &display_name,
                    &revision_id,
                    &metadata.0,
                )
                .await?,
        ))
    }

    /// Retires production publication without deleting ontology history.
    async fn retire_ontology(
        #[graphql(context)] ctx: &AppContext,
        ontology_id: String,
        publication_id: String,
        revision_id: String,
    ) -> FieldResult<bool> {
        ctx.control_plane
            .retire_ontology(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &ontology_id,
                &publication_id,
                &revision_id,
            )
            .await?;
        Ok(true)
    }

    /// Creates a pipeline with an immutable root revision.
    async fn create_pipeline(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: String,
        name: String,
        definition: DynamicJson,
        message: String,
    ) -> FieldResult<GqlPipelineDefinition> {
        Ok(GqlPipelineDefinition(
            ctx.pipelines
                .create(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &pipeline_id,
                    &name,
                    &definition.0,
                    &message,
                )
                .await?,
        ))
    }

    /// Appends a validated pipeline revision under optimistic concurrency.
    async fn update_pipeline(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: String,
        expected_head_revision_id: String,
        name: String,
        definition: DynamicJson,
        message: String,
    ) -> FieldResult<GqlPipelineDefinition> {
        Ok(GqlPipelineDefinition(
            ctx.pipelines
                .update(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &pipeline_id,
                    &expected_head_revision_id,
                    &name,
                    &definition.0,
                    &message,
                )
                .await?,
        ))
    }

    /// Publishes only the selected current pipeline head to runtime S3.
    async fn publish_pipeline(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: String,
        revision_id: String,
    ) -> FieldResult<GqlPipelineDefinition> {
        Ok(GqlPipelineDefinition(
            ctx.pipelines
                .publish(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &pipeline_id,
                    &revision_id,
                )
                .await?,
        ))
    }

    /// Retires runtime discovery and soft-deletes a pipeline definition.
    async fn delete_pipeline(
        #[graphql(context)] ctx: &AppContext,
        pipeline_id: String,
        expected_head_revision_id: String,
    ) -> FieldResult<bool> {
        ctx.pipelines
            .delete(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &pipeline_id,
                &expected_head_revision_id,
            )
            .await?;
        Ok(true)
    }

    async fn answer_pengolo_question(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::AnswerInput,
    ) -> FieldResult<bool> {
        ctx.pengolo
            .answer_question(&pengolo_actor(ctx)?, pengolo_input(input)?)
            .await
            .map_err(pengolo::question_error)
    }

    async fn send_pengolo(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::SendInput,
    ) -> FieldResult<pengolo::SendResult> {
        pengolo_result(
            ctx.pengolo
                .send(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn update_pengolo_openai(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::OpenaiInput,
    ) -> FieldResult<pengolo::Openai> {
        pengolo_result(
            ctx.pengolo
                .update_openai_account(
                    &pengolo_actor(ctx)?,
                    input.operation.as_str(),
                    input.registration.as_deref(),
                )
                .await,
        )
    }

    async fn cancel_pengolo(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::CancelInput,
    ) -> FieldResult<bool> {
        pengolo_result(
            ctx.pengolo
                .cancel(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn save_pengolo_project(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::ProjectInput,
    ) -> FieldResult<pengolo::Project> {
        pengolo_result(
            ctx.pengolo
                .save_project(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn index_document(
        #[graphql(context)] ctx: &AppContext,
        id: String,
    ) -> FieldResult<bool> {
        pengolo_result(
            ctx.pengolo.index_document(&pengolo_actor(ctx)?, &id).await,
        )
    }

    async fn save_pengolo_agent(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::AgentInput,
    ) -> FieldResult<pengolo::Agent> {
        pengolo_result(
            ctx.pengolo
                .save_agent(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn delete_pengolo_agent(
        #[graphql(context)] ctx: &AppContext,
        id: String,
    ) -> FieldResult<bool> {
        pengolo_result(
            ctx.pengolo.delete_agent(&pengolo_actor(ctx)?, &id).await,
        )
    }

    async fn update_pengolo_chat(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::ChatInput,
    ) -> FieldResult<pengolo::Chat> {
        pengolo_result(
            ctx.pengolo
                .update_chat(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn close_pengolo_temporary(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::TemporaryInput,
    ) -> FieldResult<bool> {
        pengolo_result(
            ctx.pengolo
                .close_temporary(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn delete_pengolo_chat(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::ChatInput,
    ) -> FieldResult<bool> {
        pengolo_result(
            ctx.pengolo
                .delete_chat(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn begin_pengolo_upload(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::UploadInput,
    ) -> FieldResult<pengolo::Upload> {
        pengolo_result(
            ctx.pengolo
                .begin_upload(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn finish_pengolo_upload(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::FileInput,
    ) -> FieldResult<pengolo::File> {
        pengolo_result(
            ctx.pengolo
                .finish_upload(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn delete_pengolo_file(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::FileInput,
    ) -> FieldResult<bool> {
        pengolo_result(
            ctx.pengolo
                .delete_file(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    async fn pengolo_download(
        #[graphql(context)] ctx: &AppContext,
        input: pengolo::FileInput,
    ) -> FieldResult<pengolo::Download> {
        pengolo_result(
            ctx.pengolo
                .download(&pengolo_actor(ctx)?, pengolo_input(input)?)
                .await,
        )
    }

    /// Creates an owned conversation with durable PostgreSQL state.
    async fn create_conversation(
        #[graphql(context)] ctx: &AppContext,
        title: String,
    ) -> FieldResult<GqlConversation> {
        Ok(GqlConversation(
            ctx.conversations
                .create_conversation(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &title,
                )
                .await?,
        ))
    }

    /// Updates a conversation title under optimistic concurrency control.
    async fn update_conversation(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        expected_revision: String,
        title: String,
    ) -> FieldResult<GqlConversation> {
        Ok(GqlConversation(
            ctx.conversations
                .update_conversation(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &conversation_id,
                    parse_expected_revision(&expected_revision)?,
                    &title,
                )
                .await?,
        ))
    }

    /// Soft-deletes a conversation while retaining immutable history.
    async fn delete_conversation(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        expected_revision: String,
    ) -> FieldResult<bool> {
        ctx.conversations
            .delete_conversation(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &conversation_id,
                parse_expected_revision(&expected_revision)?,
            )
            .await?;
        Ok(true)
    }

    /// Persists a user message without invoking Scribe.
    async fn create_message(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        content: String,
        attachments: Option<Vec<GqlMessageAttachmentInput>>,
    ) -> FieldResult<GqlConversationMessage> {
        let attachments = message_attachments(attachments)?;
        Ok(GqlConversationMessage(
            ctx.conversations
                .create_message(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &conversation_id,
                    &content,
                    &attachments,
                )
                .await?,
        ))
    }

    /// Replaces an owned user message and its attachment set.
    async fn update_message(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        message_id: String,
        expected_revision: String,
        content: String,
        attachments: Option<Vec<GqlMessageAttachmentInput>>,
    ) -> FieldResult<GqlConversationMessage> {
        let attachments = message_attachments(attachments)?;
        Ok(GqlConversationMessage(
            ctx.conversations
                .update_message(
                    &ctx.tenant_id,
                    &ctx.user_id,
                    &ctx.authz_context,
                    &conversation_id,
                    &message_id,
                    parse_expected_revision(&expected_revision)?,
                    &content,
                    &attachments,
                )
                .await?,
        ))
    }

    /// Soft-deletes an owned user message while retaining revisions.
    async fn delete_message(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        message_id: String,
        expected_revision: String,
    ) -> FieldResult<bool> {
        ctx.conversations
            .delete_message(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &conversation_id,
                &message_id,
                parse_expected_revision(&expected_revision)?,
            )
            .await?;
        Ok(true)
    }

    /// Generates a temporary presigned PUT URL targeting the staging bucket.
    async fn request_staging_upload(
        #[graphql(context)] ctx: &AppContext,
        group_id: Option<String>,
    ) -> FieldResult<PresignedUpload> {
        let upload = ctx
            .uploads
            .request_staging_upload(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                group_id.as_deref(),
            )
            .await?;
        Ok(PresignedUpload {
            upload_url: upload.upload_url,
            staging_key: upload.staging_key,
        })
    }

    /// Finalizes an owner-only transfer from staging to production.
    async fn complete_upload(
        #[graphql(context)] ctx: &AppContext,
        staging_key: String,
        target_name: String,
        group_id: Option<String>,
    ) -> FieldResult<String> {
        let issuer = ctx.authn_issuer.as_deref().unwrap_or_default();
        Ok(ctx
            .uploads
            .complete_upload(
                &ctx.tenant_id,
                &ctx.user_id,
                issuer,
                &ctx.authz_context,
                UploadDestination {
                    staging_key: &staging_key,
                    target_name: &target_name,
                    group_id: group_id.as_deref(),
                },
            )
            .await?)
    }
}

pub struct Subscription;

#[derive(Debug, Clone, Copy, juniper::GraphQLEnum)]
#[graphql(name = "ConversationStreamEventKind")]
pub enum GqlConversationStreamEventKind {
    Content,
    Reasoning,
}

pub struct GqlConversationStreamEvent {
    message_id: String,
    response_message_id: String,
    kind: GqlConversationStreamEventKind,
    content: String,
}

#[graphql_object(name = "ConversationStreamEvent", context = AppContext)]
impl GqlConversationStreamEvent {
    /// Returns the durable user message identifier for this generation.
    fn message_id(&self) -> &str {
        &self.message_id
    }

    /// Returns the durable assistant message identifier for terminal content.
    fn response_message_id(&self) -> &str {
        &self.response_message_id
    }

    /// Distinguishes model content from optional transient reasoning.
    fn kind(&self) -> GqlConversationStreamEventKind {
        self.kind
    }

    /// Returns one ordered stream fragment.
    fn content(&self) -> &str {
        &self.content
    }
}

type ConversationEventStream = Pin<
    Box<
        dyn Stream<Item = Result<GqlConversationStreamEvent, FieldError>>
            + Send,
    >,
>;

#[graphql_subscription(context = AppContext)]
impl Subscription {
    async fn pengolo_stream(
        #[graphql(context)] ctx: &AppContext,
        generation_id: String,
        after: i32,
        temporary: Option<bool>,
    ) -> Pin<Box<dyn Stream<Item = Result<pengolo::Replay, FieldError>> + Send>>
    {
        if let Err(error) = ctx.verify_authentication() {
            return Box::pin(futures::stream::once(async move {
                Err(FieldError::from(error))
            }));
        }
        let service = Arc::clone(&ctx.pengolo);
        let tenant = ctx.tenant_id.clone();
        let user = ctx.user_id.clone();
        let context = ctx.authz_context.clone();
        let expires = ctx.authn_expires_at;
        let temporary = temporary.unwrap_or(false);
        Box::pin(async_stream::stream! {
            let mut changes = service.output_changes();
            let actor=crate::application::usecases::pengolo::Principal {tenant:&tenant,user:&user,context:&context};let mut cursor=after;let mut status=String::new();
            loop {
                if usize::try_from(chrono::Utc::now().timestamp()).map_or(true,|now|now>=expires) {yield Err(FieldError::new("Authentication expired",juniper::graphql_value!({"code":"AUTHENTICATION_EXPIRED"})));break;}
                match service.events(&actor,&generation_id,cursor,temporary).await {
                    Ok(page)=> {
                        let next=page.get("events").and_then(Value::as_array).and_then(|events|events.last()).and_then(|event|event.get("sequence")).and_then(Value::as_i64).and_then(|sequence|i32::try_from(sequence).ok()).unwrap_or(cursor);
                        let next_status=page.get("status").and_then(Value::as_str).unwrap_or("failed").to_owned();let terminal=matches!(next_status.as_str(),"completed"|"failed"|"cancelled");
                        if next>cursor || status!=next_status {cursor=next;status=next_status;yield pengolo_result(Ok(page));}
                        if terminal {break;}
                    },
                    Err(_)=>{yield Err(FieldError::new("Generation access unavailable",juniper::graphql_value!({"code":"FORBIDDEN"})));break;}
                }
                // Subscribe before reading so a committed fragment cannot fall between replay and waiting.
                crate::application::usecases::pengolo::wait_for_output(&mut changes, &generation_id).await;
            }
        })
    }

    async fn pengolo_identity_changes(
        #[graphql(context)] ctx: &AppContext,
    ) -> Pin<
        Box<dyn Stream<Item = Result<pengolo::Identity, FieldError>> + Send>,
    > {
        if let Err(error) = ctx.verify_authentication() {
            return Box::pin(futures::stream::once(async move {
                Err(FieldError::from(error))
            }));
        }
        let service = Arc::clone(&ctx.pengolo);
        let tenant = ctx.tenant_id.clone();
        let user = ctx.user_id.clone();
        let context = ctx.authz_context.clone();
        let expires = ctx.authn_expires_at;
        Box::pin(async_stream::stream! {
            let mut changes = service.identity_changes();
            let actor = crate::application::usecases::pengolo::Principal {tenant: &tenant, user: &user, context: &context};
            let mut version = String::new();
            loop {
                if usize::try_from(chrono::Utc::now().timestamp()).map_or(true, |now| now >= expires) {
                    yield Err(FieldError::new("Authentication expired", juniper::graphql_value!({"code":"AUTHENTICATION_EXPIRED"})));
                    break;
                }
                match service.identity(&actor).await {
                    Ok(identity) => {
                        let next = identity.get("version").and_then(Value::as_str).unwrap_or_default();
                        if version != next {
                            version = next.to_owned();
                            yield pengolo_result(Ok(identity));
                        }
                    }
                    Err(_) => {
                        yield Err(FieldError::new("Workspace access unavailable", juniper::graphql_value!({"code":"FORBIDDEN"})));
                        break;
                    }
                }
                // Other replicas and external IAM writers are reconciled without client polling.
                tokio::select! {
                    result = changes.changed() => { if result.is_err() { break; } }
                    () = tokio::time::sleep(std::time::Duration::from_secs(30)) => {}
                }
            }
        })
    }

    /// Persists a user message, streams Scribe, and persists terminal output.
    async fn ask(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        prompt: String,
        model_alias: Option<String>,
        reasoning_effort: Option<String>,
        attachments: Option<Vec<GqlMessageAttachmentInput>>,
    ) -> ConversationEventStream {
        if let Err(error) = ctx.verify_authentication() {
            return Box::pin(futures::stream::once(async move {
                Err(FieldError::from(error))
            }));
        }
        let attachments = match message_attachments(attachments) {
            Ok(attachments) => attachments,
            Err(error) => {
                return Box::pin(futures::stream::once(async { Err(error) }));
            },
        };
        let generation = match ctx
            .conversations
            .ask(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &conversation_id,
                &prompt,
                model_alias.as_deref(),
                reasoning_effort.as_deref(),
                &attachments,
            )
            .await
        {
            Ok(generation) => generation,
            Err(error) => {
                return Box::pin(futures::stream::once(async move {
                    Err(FieldError::from(error))
                }));
            },
        };
        let message_id = Arc::new(generation.message_id);
        let response_message_id = Arc::new(generation.response_message_id);
        Box::pin(generation.stream.map(move |result| {
            let message_id = Arc::clone(&message_id);
            let response_message_id = Arc::clone(&response_message_id);
            result
                .map(|chunk| {
                    let (kind, content) = match chunk {
                        AgentChunk::Content(content) => {
                            (GqlConversationStreamEventKind::Content, content)
                        },
                    };
                    GqlConversationStreamEvent {
                        message_id: (*message_id).clone(),
                        response_message_id: (*response_message_id).clone(),
                        kind,
                        content,
                    }
                })
                .map_err(FieldError::from)
        }))
    }
}

pub type AppSchema = RootNode<Query, Mutation, Subscription>;

/// Creates the stable query, mutation, and subscription root schema.
pub fn create_schema() -> AppSchema {
    AppSchema::new(Query, Mutation, Subscription)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct PipelineErrorContract;

    #[graphql_object]
    impl PipelineErrorContract {
        fn pipeline_definition() -> FieldResult<Option<String>> {
            Err(pipeline_error(
                crate::application::usecases::pipelines::PipelineAccessDenied
                    .into(),
            ))
        }

        fn pipeline_composition() -> FieldResult<Option<String>> {
            Err(pipeline_error(anyhow::anyhow!(
                "private storage endpoint and credential"
            )))
        }
    }

    #[test]
    fn pipeline_errors_preserve_codes_and_paths_without_dependency_details()
    -> anyhow::Result<()> {
        let schema = RootNode::new(
            PipelineErrorContract,
            juniper::EmptyMutation::<()>::new(),
            juniper::EmptySubscription::<()>::new(),
        );
        let (_, errors) = juniper::execute_sync(
            "{ pipelineDefinition pipelineComposition }",
            None,
            &schema,
            &juniper::Variables::new(),
            &(),
        )?;
        let errors = serde_json::to_value(errors)?;
        assert_eq!(
            errors.pointer("/0/path/0"),
            Some(&serde_json::json!("pipelineDefinition"))
        );
        assert_eq!(
            errors.pointer("/0/extensions/code"),
            Some(&serde_json::json!("FORBIDDEN"))
        );
        assert_eq!(
            errors.pointer("/1/path/0"),
            Some(&serde_json::json!("pipelineComposition"))
        );
        assert_eq!(
            errors.pointer("/1/extensions/code"),
            Some(&serde_json::json!("PIPELINE_COMPOSITION_INVALID"))
        );
        anyhow::ensure!(!errors.to_string().contains("private storage"));
        Ok(())
    }

    #[test]
    fn pipeline_composition_queries_use_native_authorized_contracts()
    -> anyhow::Result<()> {
        let schema = create_schema();
        let source = "query($definition: JSON!) { pipelineDefinition(pipelineId: \"example\", revisionId: \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\") { pipelineId definition } pipelineLinkTargets(limit: 100) { pipelineId publishedRevisionId } pipelineComposition(pipelineId: \"draft\", definition: $definition) { definition blocks transfers } }";
        let document =
            juniper::parser::parse_document_source(source, &schema.schema)
                .map_err(|error| anyhow::anyhow!("{error:?}"))?;
        let mut context = juniper::validation::ValidatorContext::new(
            &schema.schema,
            &document,
        );
        juniper::validation::rules::visit_all_rules(&mut context, &document);
        let errors = context.into_errors();
        anyhow::ensure!(errors.is_empty(), "{errors:?}");
        Ok(())
    }

    #[test]
    fn capabilities_graphql_queries_are_typed_and_filterable()
    -> anyhow::Result<()> {
        let schema = create_schema();
        let source = "{ pipelineCapabilities(kind: INPUT, pluginId: \"core\") { schemaVersion capabilities { id pluginId pluginVersion kind targets name description parametersSchemaJson stepType modelClass } } scribeCapabilities(kind: MCP) { schemaVersion capabilities { id kind targets } } }";
        let document =
            juniper::parser::parse_document_source(source, &schema.schema)
                .map_err(|error| anyhow::anyhow!("{error:?}"))?;
        let mut context = juniper::validation::ValidatorContext::new(
            &schema.schema,
            &document,
        );
        juniper::validation::rules::visit_all_rules(&mut context, &document);
        let errors = context.into_errors();
        anyhow::ensure!(errors.is_empty(), "{errors:?}");
        Ok(())
    }

    #[test]
    fn pengolo_dashboard_documents_validate_against_typed_schema()
    -> anyhow::Result<()> {
        let schema = create_schema();
        let source = include_str!(
            "../../../../../../front/dashboard/app/graphql/pengolo.graphql"
        );
        let document =
            juniper::parser::parse_document_source(source, &schema.schema)
                .map_err(|error| anyhow::anyhow!("{error:?}"))?;
        let mut context = juniper::validation::ValidatorContext::new(
            &schema.schema,
            &document,
        );
        juniper::validation::rules::visit_all_rules(&mut context, &document);
        let errors = context.into_errors();
        anyhow::ensure!(errors.is_empty(), "{errors:?}");
        Ok(())
    }

    #[test]
    fn pengolo_typed_schema_supports_selected_fields_and_paginated_messages() {
        let schema = create_schema().as_sdl();
        for expected in [
            "type PengoloChat {",
            "pengoloChat(id: String!, temporary: Boolean): PengoloChat\n",
            "type PengoloChatData {",
            "messages(before: String, limit: Int): PengoloMessagePage!",
            "type PengoloMessage {",
            "type PengoloReplay {",
            "message: PengoloMessage",
            "sendPengolo(input: PengoloSendInput!): PengoloSendResult!",
        ] {
            assert!(
                schema.contains(expected),
                "Missing typed contract: {expected}"
            );
        }
    }

    struct IdentityContractQuery;

    #[graphql_object]
    impl IdentityContractQuery {
        fn payload() -> pengolo::Identity {
            pengolo::Identity {
                version: "revision-1".into(),
                web_search_available: true,
                administrator: false,
            }
        }
    }

    #[test]
    fn pengolo_typed_identity_is_a_selected_object() -> anyhow::Result<()> {
        let schema = RootNode::new(
            IdentityContractQuery,
            juniper::EmptyMutation::<()>::new(),
            juniper::EmptySubscription::<()>::new(),
        );
        let (result, errors) = juniper::execute_sync(
            "{ payload { version webSearchAvailable } }",
            None,
            &schema,
            &juniper::Variables::new(),
            &(),
        )?;
        anyhow::ensure!(errors.is_empty(), "Identity resolution failed");
        assert_eq!(
            serde_json::to_value(result)?,
            serde_json::json!({"payload":{"version":"revision-1","webSearchAvailable":true}})
        );
        Ok(())
    }

    #[test]
    fn e2e_administration_documents_validate_against_native_schema()
    -> anyhow::Result<()> {
        let schema = create_schema();
        let sources: [(&str, &[&str]); 2] = [
            (
                include_str!(
                    "../../../../../../tests/e2e/test_pipeline_lifecycle.py"
                ),
                &[
                    "TenantAdmin",
                    "Directory",
                    "MemberDirectory",
                    "RevokedAdmin",
                    "PublicQueries",
                ],
            ),
            (
                include_str!(
                    "../../../../../../tests/e2e/empirical_pipeline.py"
                ),
                &["EmpiricalDirectory"],
            ),
        ];
        let mut failures = Vec::new();
        for (source, names) in sources {
            for name in names {
                let marker = format!("query {name}");
                let start = source.find(&marker).ok_or_else(|| {
                    anyhow::anyhow!("Missing E2E operation {name}")
                })?;
                let tail = source.get(start..).ok_or_else(|| {
                    anyhow::anyhow!("Invalid operation position")
                })?;
                let mut depth = 0_usize;
                let mut quoted = false;
                let mut escaped = false;
                let mut end = None;
                // Read the exact Python fixture operation, including multiline
                // selections and quoted arguments.
                for (index, character) in tail.char_indices() {
                    if escaped {
                        escaped = false;
                        continue;
                    }
                    if quoted && character == '\\' {
                        escaped = true;
                        continue;
                    }
                    if character == '"' {
                        quoted = !quoted;
                        continue;
                    }
                    if quoted {
                        continue;
                    }
                    if character == '{' {
                        depth += 1;
                    }
                    if character == '}' {
                        depth = depth.checked_sub(1).ok_or_else(|| {
                            anyhow::anyhow!("Unbalanced E2E operation {name}")
                        })?;
                        if depth == 0 {
                            end = Some(index + 1);
                            break;
                        }
                    }
                }
                let operation = tail
                    .get(
                        ..end.ok_or_else(|| {
                            anyhow::anyhow!("Unclosed E2E operation {name}")
                        })?,
                    )
                    .ok_or_else(|| {
                        anyhow::anyhow!("Invalid E2E operation {name}")
                    })?;
                let document = juniper::parser::parse_document_source(
                    operation,
                    &schema.schema,
                )
                .map_err(|error| anyhow::anyhow!("{name}: {error:?}"))?;
                let mut context = juniper::validation::ValidatorContext::new(
                    &schema.schema,
                    &document,
                );
                juniper::validation::rules::visit_all_rules(
                    &mut context,
                    &document,
                );
                failures.extend(
                    context
                        .into_errors()
                        .into_iter()
                        .map(|error| format!("{name}: {}", error.message())),
                );
            }
        }
        anyhow::ensure!(failures.is_empty(), "{}", failures.join("\n"));
        Ok(())
    }

    #[test]
    fn dashboard_administration_documents_validate_against_native_schema()
    -> anyhow::Result<()> {
        let schema = create_schema();
        let sources = [
            (
                "settings/sso.vue",
                include_str!(
                    "../../../../../../front/dashboard/app/pages/settings/sso.vue"
                ),
            ),
            (
                "Administration/Permissions.vue",
                include_str!(
                    "../../../../../../front/dashboard/app/components/Administration/Permissions.vue"
                ),
            ),
            (
                "useCommandPaletteSearch.ts",
                include_str!(
                    "../../../../../../front/dashboard/app/composables/useCommandPaletteSearch.ts"
                ),
            ),
        ];
        let sources = sources.into_iter().chain([
            (
                "useAdministration.ts",
                include_str!("../../../../../../front/dashboard/app/composables/useAdministration.ts"),
            ),
            (
                "usePermissions.ts",
                include_str!("../../../../../../front/dashboard/app/composables/usePermissions.ts"),
            ),
        ]);
        let mut failures = Vec::new();
        let mut checked = 0;
        for (name, source) in sources {
            for operation in source
                .lines()
                .filter_map(|line| {
                    let start = line.find(['"', '\''])?;
                    let quoted = line.get(start..)?;
                    let delimiter = quoted.chars().next()?;
                    let content = quoted.strip_prefix(delimiter)?;
                    content.rsplit_once(delimiter).map(|(value, _)| value)
                })
                .filter(|value| {
                    value.starts_with("query ")
                        || value.starts_with("query(")
                        || value.starts_with("mutation(")
                        || value.starts_with("mutation ")
                })
            {
                let document = juniper::parser::parse_document_source(
                    operation,
                    &schema.schema,
                )
                .map_err(|error| anyhow::anyhow!("{name}: {error:?}"))?;
                let mut context = juniper::validation::ValidatorContext::new(
                    &schema.schema,
                    &document,
                );
                juniper::validation::rules::visit_all_rules(
                    &mut context,
                    &document,
                );
                failures.extend(context.into_errors().into_iter().map(
                    |error| {
                        format!("{name}: {} ({operation})", error.message())
                    },
                ));
                checked += 1;
            }
        }
        anyhow::ensure!(
            checked >= 20,
            "Administration documents must remain visible to the native schema contract test"
        );
        anyhow::ensure!(failures.is_empty(), "{}", failures.join("\n"));
        Ok(())
    }

    #[test]
    fn administration_errors_preserve_recovery_codes_without_storage_details()
    {
        let denied = administration_error(anyhow::anyhow!(
            "You cannot suspend or delete your own account"
        ));
        assert!(denied.message().contains("Ask another administrator"));
        assert!(
            format!("{:?}", denied.extensions()).contains("SELF_SUSPENSION")
        );
        let internal = administration_error(anyhow::anyhow!(
            "SQL failed for postgres://secret@database and private table"
        ));
        assert!(!internal.message().contains("postgres"));
        assert!(!internal.message().contains("secret"));
        assert!(
            format!("{:?}", internal.extensions()).contains("INTERNAL_ERROR")
        );
    }

    #[test]
    fn administration_contracts_are_typed_and_paginated() {
        let schema = create_schema().as_sdl();
        for expected in [
            "ssoSettings: SsoSettings!",
            "type SsoSettings {",
            "organizationName: String!",
            "roleSyncConfigurations: [RoleSyncConfiguration!]!",
            "cedarPolicies: [CedarPolicy!]!",
            "type UserPage {",
            "type RolePage {",
            "after: String",
            "accessGraph(search: String, limit: Int): AccessGraph!",
            "type SearchPage {",
            "navigation: [NavigationHit!]!",
            "type NavigationHit {",
        ] {
            assert!(
                schema.contains(expected),
                "Missing typed contract: {expected}"
            );
        }
        assert!(!schema.contains("ssoSettings: JSON"));
    }

    #[test]
    fn administration_is_grouped_at_both_graphql_roots() {
        let schema = create_schema().as_sdl();
        for (root, nested) in [
            ("Query", "AdministrationQuery"),
            ("Mutation", "AdministrationMutation"),
        ] {
            let body = schema
                .split(&format!("type {root} {{"))
                .nth(1)
                .and_then(|tail| tail.split('}').next())
                .unwrap_or_default();
            assert!(body.contains(&format!("administration: {nested}!")));
            for orphan in [
                "ssoSettings",
                "setPasskeyMfa",
                "updateOrganization",
                "users(",
                "roles(",
                "setCedarPolicy",
            ] {
                assert!(
                    !body.contains(orphan),
                    "orphan administration field on {root}: {orphan}"
                );
            }
        }
        assert!(schema.contains(
            "accessGraph(search: String, limit: Int): AccessGraph!"
        ));
        assert!(schema.contains(
            "users(search: String, limit: Int, after: String): UserPage!"
        ));
        assert!(schema.contains(
            "roles(search: String, limit: Int, after: String): RolePage!"
        ));
    }

    #[test]
    fn schema_exposes_one_consistent_control_plane_vocabulary() {
        let schema = create_schema().as_sdl();
        for expected in [
            "scribeModels: ScribeModelCatalog!",
            "type ScribeModel",
            "reasoningEfforts: [String!]!",
            "defaultReasoningEffort: String",
            "reasoningEffort: String",
            "type AuditEvent",
            "type Ontology",
            "type OntologyPublication",
            "type PipelineExecution",
            "type PipelineOntologyBinding",
            "type RoleAssignment",
            "auditEvents(",
            "ontologies(",
            "ontologyBindings(",
            "pipelineExecutions(",
            "structuredSearch(",
            "input: StructuredSearchInput!",
            "assignRoleToUser(",
            "setDataDomainGrant(",
            "enum DataDomainGrant",
            "unassignRoleFromUser(",
            "conversations(",
            "conversation(",
            "createConversation(",
            "updateConversation(",
            "deleteConversation(",
            "createMessage(",
            "updateMessage(",
            "deleteMessage(",
            "pipelineDefinitions(",
            "createPipeline(",
            "updatePipeline(",
            "deletePipeline(",
            "publishPipeline(",
            "updateOrganization(name: String!): Boolean!",
            "detectSsoProvider(domain: String!, address: String!): SsoProviderPreview!",
            "updateUser(",
            "deleteUser(",
            "deleteRole(",
            "conversationId",
            "messageId",
            "publishOntology(",
            "retireOntology(",
        ] {
            assert!(
                schema.contains(expected),
                "missing schema contract: {expected}"
            );
        }
        assert!(!schema.contains("ontologyVersion"));
        assert!(!schema.contains("workflow"));
        assert!(schema.contains(
            "retireOntology(ontologyId: String!, publicationId: String!, revisionId: String!): Boolean!"
        ));
    }
}
