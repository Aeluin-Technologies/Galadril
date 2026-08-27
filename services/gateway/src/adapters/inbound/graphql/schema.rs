//! GraphQL schema definition mapping to application use cases with FGAC.

use std::pin::Pin;
use std::sync::Arc;

use futures::{Stream, StreamExt as _};
use juniper::{
    FieldError, FieldResult, RootNode, graphql_object, graphql_scalar,
    graphql_subscription,
};
use serde_json::Value;

use crate::adapters::inbound::graphql::context::AppContext;
use crate::application::ports::audit_store::{
    AuditEvent, AuditFilter, AuditOutcome,
};
use crate::application::ports::control_plane_store::{
    OntologyCatalogEntry, OntologyPublication, PipelineExecution,
    PipelineOntologyBinding,
};
use crate::application::ports::conversation_agent::AgentChunk;
use crate::application::ports::conversation_store::{
    AttachmentKind, Conversation, ConversationMessage, MessageAttachment,
};
use crate::application::ports::iam_store::{IamRole, IamUser, RoleAssignment};
use crate::application::ports::pipeline_store::PipelineDefinition;
use crate::application::usecases::search::{
    GlobalSearchHit, StructuredSearchQuery,
};

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
}

impl From<GqlAttachmentKind> for AttachmentKind {
    /// Converts the public enum into the canonical persistence value.
    fn from(value: GqlAttachmentKind) -> Self {
        match value {
            GqlAttachmentKind::Image => Self::Image,
            GqlAttachmentKind::Audio => Self::Audio,
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

#[graphql_object(name = "Role", context = AppContext)]
impl GqlRole {
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
        GlobalSearchHit::EntityState { entity_id, state } => {
            GqlGlobalSearchHit {
                kind: "entity_state".to_string(),
                entity_id: Some(entity_id),
                event_id: None,
                event_type: None,
                modality: None,
                created_at_ms: None,
                event_time_ms: None,
                score: None,
                payload: state,
            }
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

pub struct Query;

#[graphql_object(context = AppContext)]
impl Query {
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

    /// Explicit event search (developer-facing). Returns raw JSON rows.
    async fn search_events(
        #[graphql(context)] ctx: &AppContext,
        event_type: Option<String>,
        text: Option<String>,
        limit: Option<i32>,
    ) -> FieldResult<Vec<DynamicJson>> {
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
            .map(|e| {
                DynamicJson(serde_json::json!({
                    "event_id": e.event_id,
                    "event_type": e.event_type,
                    "event_time_ms": i64_ms_to_f64(e.event_time_ms),
                    "properties": e.properties
                }))
            })
            .collect())
    }

    /// Explicit embedding search (developer-facing). Uses text->embedding
    /// (fake for now).
    async fn search_embeddings(
        #[graphql(context)] ctx: &AppContext,
        query_text: String,
        modality: Option<String>,
        k: Option<i32>,
    ) -> FieldResult<Vec<DynamicJson>> {
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
            .map(|r| {
                DynamicJson(serde_json::json!({
                    "id": r.id,
                    "entity_id": r.entity_id,
                    "modality": r.modality,
                    "created_at_ms": i64_ms_to_f64(r.created_at_ms),
                    "metadata": r.metadata,
                    "score": r.score as f64
                }))
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
                label: n.label,
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

    /// Lists tenant users for authorized tenant administrators.
    async fn users(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlUser>> {
        let users = ctx
            .control_plane
            .users(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await?;
        Ok(users.into_iter().map(GqlUser).collect())
    }

    /// Lists tenant roles for authorized tenant administrators.
    async fn roles(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlRole>> {
        let roles = ctx
            .control_plane
            .roles(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await?;
        Ok(roles.into_iter().map(GqlRole).collect())
    }

    /// Lists durable role memberships for authorized tenant administrators.
    async fn role_assignments(
        #[graphql(context)] ctx: &AppContext,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlRoleAssignment>> {
        let assignments = ctx
            .control_plane
            .role_assignments(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                control_plane_limit(limit),
            )
            .await?;
        Ok(assignments.into_iter().map(GqlRoleAssignment).collect())
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
            .await?;
        Ok(events.into_iter().map(GqlAuditEvent).collect())
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

    /// Lists immutable publication history for one authorized ontology.
    async fn ontology_publication_history(
        #[graphql(context)] ctx: &AppContext,
        ontology_id: String,
        limit: Option<i32>,
    ) -> FieldResult<Vec<GqlOntologyPublication>> {
        let publications = ctx
            .control_plane
            .ontology_publication_history(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
                &ontology_id,
                control_plane_limit(limit),
            )
            .await?;
        Ok(publications
            .into_iter()
            .map(GqlOntologyPublication)
            .collect())
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

pub struct Mutation;

#[graphql_object(context = AppContext)]
impl Mutation {
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
            .await?;
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
            .await?;
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
            .await?;
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
            .await?;
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
            .await?;
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
            .await?;
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
            .await?;
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
            .await?;
        Ok(true)
    }

    /// Generates a temporary presigned PUT URL targeting the staging bucket.
    async fn request_staging_upload(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<PresignedUpload> {
        let upload = ctx
            .uploads
            .request_staging_upload(
                &ctx.tenant_id,
                &ctx.user_id,
                &ctx.authz_context,
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
    ) -> FieldResult<String> {
        let issuer = ctx.authn_issuer.as_deref().unwrap_or_default();
        Ok(ctx
            .uploads
            .complete_upload(
                &ctx.tenant_id,
                &ctx.user_id,
                issuer,
                &ctx.authz_context,
                &staging_key,
                &target_name,
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
    /// Persists a user message, streams Scribe, and persists terminal output.
    async fn ask(
        #[graphql(context)] ctx: &AppContext,
        conversation_id: String,
        prompt: String,
        model_alias: Option<String>,
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
                        AgentChunk::Reasoning(content) => (
                            GqlConversationStreamEventKind::Reasoning,
                            content,
                        ),
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

    #[test]
    fn schema_exposes_one_consistent_control_plane_vocabulary() {
        let schema = create_schema().as_sdl();
        for expected in [
            "type AuditEvent",
            "type Ontology",
            "type OntologyPublication",
            "type PipelineExecution",
            "type PipelineOntologyBinding",
            "type RoleAssignment",
            "auditEvents(",
            "ontologies(",
            "ontologyPublicationHistory(",
            "ontologyBindings(",
            "pipelineExecutions(",
            "structuredSearch(",
            "input: StructuredSearchInput!",
            "assignRoleToUser(",
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
