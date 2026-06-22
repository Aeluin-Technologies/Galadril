//! GraphQL schema definition mapping to application use cases with FGAC.

use std::pin::Pin;

use futures::Stream;
use juniper::{
    FieldError, FieldResult, RootNode, graphql_object, graphql_scalar,
    graphql_subscription,
};
use serde_json::Value;

use crate::adapters::inbound::graphql::context::AppContext;
use crate::application::ports::iam_store::IamStore;
use crate::application::usecases::search::GlobalSearchHit;
use crate::domain::key::sanitize_upload_request;
use crate::domain::permission::{Effect, IamPermission};

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

    pub fn to_output<S: ScalarValue>(v: &DynamicJson) -> S {
        S::from_displayable_non_static(&v.0)
    }

    pub fn from_input<S: ScalarValue>(
        v: &juniper::Scalar<S>,
    ) -> Result<DynamicJson, String> {
        v.try_as_str()
            .ok_or_else(|| format!("Expected a string for JSON, found: {}", v))
            .and_then(|s| serde_json::from_str(s).map_err(|e| e.to_string()))
            .map(DynamicJson)
    }

    pub fn parse_token<S: ScalarValue>(
        value: ScalarToken<'_>,
    ) -> ParseScalarResult<S> {
        <String as juniper::ParseScalarValue<S>>::from_str(value)
    }
}

pub struct GqlSinkMetadata {
    name: String,
    columns: Vec<String>,
}

#[graphql_object(name = "TableMetadata", context = AppContext)]
impl GqlSinkMetadata {
    fn name(&self) -> &str {
        &self.name
    }

    fn columns(&self) -> &[String] {
        &self.columns
    }
}

/// Input object for applying Fine-Grained Access Control and filtering.
#[derive(juniper::GraphQLInputObject)]
pub struct GqlQueryFilters {
    pub entity_id: Option<String>,
    pub modality: Option<String>,
    pub state_type: Option<String>,
    pub gis_zone: Option<String>,
}

/// Search hit result (permission-filtered).
pub struct GqlSearchHit {
    entity_id: String,
    metadata: Value,
}

#[graphql_object(name = "SearchHit", context = AppContext)]
impl GqlSearchHit {
    fn entity_id(&self) -> &str {
        &self.entity_id
    }

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

#[graphql_object(name = "GlobalSearchHit", context = AppContext)]
impl GqlGlobalSearchHit {
    fn kind(&self) -> &str {
        &self.kind
    }

    fn entity_id(&self) -> Option<&str> {
        self.entity_id.as_deref()
    }

    fn event_id(&self) -> Option<&str> {
        self.event_id.as_deref()
    }

    fn event_type(&self) -> Option<&str> {
        self.event_type.as_deref()
    }

    fn modality(&self) -> Option<&str> {
        self.modality.as_deref()
    }

    fn created_at_ms(&self) -> Option<f64> {
        self.created_at_ms
    }

    fn event_time_ms(&self) -> Option<f64> {
        self.event_time_ms
    }

    fn score(&self) -> Option<f64> {
        self.score
    }

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
    fn id(&self) -> &str {
        &self.id
    }

    fn label(&self) -> &str {
        &self.label
    }

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
    fn from_id(&self) -> &str {
        &self.from_id
    }

    fn to_id(&self) -> &str {
        &self.to_id
    }

    fn label(&self) -> &str {
        &self.label
    }

    fn properties(&self) -> DynamicJson {
        DynamicJson(self.properties.clone())
    }
}

pub struct GqlGraphSubgraph {
    nodes: Vec<GqlGraphNode>,
    edges: Vec<GqlGraphEdge>,
}

#[graphql_object(name = "GraphSubgraph", context = AppContext)]
impl GqlGraphSubgraph {
    fn nodes(&self) -> &[GqlGraphNode] {
        &self.nodes
    }

    fn edges(&self) -> &[GqlGraphEdge] {
        &self.edges
    }
}

/// IAM permission input.
#[derive(juniper::GraphQLInputObject)]
pub struct GqlPermissionInput {
    pub effect: String,
    pub action: String,
    /// JSON string to avoid ambiguous Juniper JSON input parsing.
    pub scope_json: String,
}

fn parse_effect(effect: &str) -> FieldResult<Effect> {
    match effect {
        "allow" => Ok(Effect::Allow),
        "deny" => Ok(Effect::Deny),
        other => Err(FieldError::new(
            format!("Unknown effect '{other}', expected 'allow'|'deny'"),
            juniper::Value::null(),
        )),
    }
}

fn parse_permission_inputs(
    inputs: Vec<GqlPermissionInput>,
) -> FieldResult<Vec<IamPermission>> {
    let mut out = Vec::with_capacity(inputs.len());
    for i in inputs {
        let effect = parse_effect(i.effect.trim())?;
        let scope: Value =
            serde_json::from_str(i.scope_json.trim()).map_err(|e| {
                FieldError::new(
                    format!("Invalid scope_json: {e}"),
                    juniper::Value::null(),
                )
            })?;

        out.push(IamPermission {
            effect,
            action: i.action,
            scope,
        });
    }
    Ok(out)
}

fn i64_ms_to_f64(ms: i64) -> f64 {
    ms as f64
}

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
    /// Legacy explicit search for entity states by name (permission-filtered).
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
            .search_entities_by_name(&ctx.tenant_id, &ctx.user_id, &query, lim)
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
            .global_search(&ctx.tenant_id, &ctx.user_id, &query, lim)
            .await
            .map_err(FieldError::from)?;

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
}

/// Represents the generated S3 direct-upload target package.
#[derive(Debug, Clone)]
pub struct PresignedUpload {
    pub upload_url: String,
    pub staging_key: String,
}

#[graphql_object(context = AppContext)]
impl PresignedUpload {
    fn upload_url(&self) -> &str {
        &self.upload_url
    }

    fn staging_key(&self) -> &str {
        &self.staging_key
    }
}

pub struct Mutation;

#[graphql_object(context = AppContext)]
impl Mutation {
    async fn create_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        is_active: Option<bool>,
    ) -> FieldResult<bool> {
        let active = is_active.unwrap_or(true);

        ctx.iam_admin
            .create_user(&ctx.tenant_id, &ctx.user_id, &user_id, active)
            .await?;
        Ok(true)
    }

    async fn create_role(
        #[graphql(context)] ctx: &AppContext,
        role_name: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .create_role(&ctx.tenant_id, &ctx.user_id, &role_name)
            .await?;
        Ok(true)
    }

    async fn assign_role_to_user(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        role_name: String,
    ) -> FieldResult<bool> {
        ctx.iam_admin
            .assign_role_to_user(
                &ctx.tenant_id,
                &ctx.user_id,
                &user_id,
                &role_name,
            )
            .await?;
        Ok(true)
    }

    async fn set_user_permissions(
        #[graphql(context)] ctx: &AppContext,
        user_id: String,
        permissions: Vec<GqlPermissionInput>,
    ) -> FieldResult<bool> {
        let perms = parse_permission_inputs(permissions)?;
        ctx.iam_admin
            .set_user_permissions(
                &ctx.tenant_id,
                &ctx.user_id,
                &user_id,
                &perms,
            )
            .await?;
        Ok(true)
    }

    async fn set_role_permissions(
        #[graphql(context)] ctx: &AppContext,
        role_name: String,
        permissions: Vec<GqlPermissionInput>,
    ) -> FieldResult<bool> {
        let perms = parse_permission_inputs(permissions)?;
        ctx.iam_admin
            .set_role_permissions(
                &ctx.tenant_id,
                &ctx.user_id,
                &role_name,
                &perms,
            )
            .await?;
        Ok(true)
    }

    /// Generates a temporary presigned PUT URL targeting the staging bucket.
    async fn request_staging_upload(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<PresignedUpload> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(|e| {
                juniper::FieldError::new(e.to_string(), juniper::Value::Null)
            })?;

        // Isolate staging keys inside the user's explicit tenant context tree.
        let unique_id = uuid::Uuid::new_v4().to_string();
        let staging_key = format!("{}/{}", ctx.tenant_id, unique_id);

        let upload_url = ctx
            .s3
            .generate_presigned_upload_url(
                &staging_key,
                std::time::Duration::from_secs(900),
            )
            .await
            .map_err(|e| {
                juniper::FieldError::new(e.to_string(), juniper::Value::Null)
            })?;

        Ok(PresignedUpload {
            upload_url,
            staging_key,
        })
    }

    /// Validates requested access envelopes, and finalizes transfer from
    /// staging to production with tagging.
    async fn complete_upload(
        #[graphql(context)] ctx: &AppContext,
        staging_key: String,
        target_name: String,
        permissions_json: Option<String>,
    ) -> FieldResult<String> {
        fn graphql_error<E: std::fmt::Display>(err: E) -> juniper::FieldError {
            juniper::FieldError::new(err.to_string(), juniper::Value::Null)
        }

        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(graphql_error)?;

        let authorized = ctx
            .auth_service
            .is_authorized(
                &ctx.user_id,
                crate::application::usecases::authorization::Permission::Write,
                "tenant",
                &ctx.tenant_id,
                None,
            )
            .await
            .map_err(graphql_error)?;

        if !authorized {
            return Err(graphql_error(
                "Unauthorized write operation on tenant",
            ));
        }

        let viewers_csv = match permissions_json
            .as_deref()
            .map(str::trim)
            .filter(|json| !json.is_empty())
        {
            Some(json) => {
                let requested_permissions: Vec<
                    crate::domain::permission::IamPermission,
                > = serde_json::from_str(json).map_err(|e| {
                    graphql_error(format!(
                        "Invalid permissions JSON structure: {e}"
                    ))
                })?;

                if requested_permissions.is_empty() {
                    None
                } else {
                    let current_permissions = ctx
                        .iam_store
                        .get_effective_permissions_for_user(
                            &ctx.tenant_id,
                            &ctx.user_id,
                        )
                        .await
                        .map_err(graphql_error)?;

                    if !crate::application::usecases::iam_scope::can_grant_all(
                        &current_permissions,
                        &requested_permissions,
                    ) {
                        return Err(graphql_error(
                            "Requested access envelope exceeds user's current authorization bounds",
                        ));
                    }

                    viewers_from_permissions(&requested_permissions)
                }
            },
            None => None,
        };

        let upload_meta =
            sanitize_upload_request(&ctx.tenant_id, None, &target_name)
                .map_err(|e| {
                    graphql_error(format!("Invalid upload parameters: {e}"))
                })?;

        let tagging_query = build_tagging_query(
            &ctx.tenant_id,
            &ctx.user_id,
            viewers_csv.as_deref(),
        );

        let destination_key = ctx
            .s3
            .finalize_object(
                &staging_key,
                &upload_meta.s3_key,
                Some(&ctx.tenant_id),
                tagging_query.as_deref(),
            )
            .await
            .map_err(graphql_error)?;

        Ok(destination_key)
    }
}

pub struct Subscription;

type StringStream =
    Pin<Box<dyn Stream<Item = Result<String, FieldError>> + Send>>;

#[graphql_subscription(context = AppContext)]
impl Subscription {
    /// AI Chat subscription.
    async fn ask(
        #[graphql(context)] ctx: &AppContext,
        prompt: String,
    ) -> StringStream {
        let user = ctx.user_id.clone();
        let tenant = ctx.tenant_id.clone();
        let stream = async_stream::stream! {
            yield Ok(format!("Hello {user}@{tenant}, you asked: {prompt}"));
        };

        Box::pin(stream)
    }
}

pub type AppSchema = RootNode<Query, Mutation, Subscription>;

pub fn create_schema() -> AppSchema {
    AppSchema::new(Query, Mutation, Subscription)
}

fn viewers_from_permissions(
    perms: &[crate::domain::permission::IamPermission],
) -> Option<String> {
    let mut out = String::new();

    for p in perms {
        if p.effect != crate::domain::permission::Effect::Allow {
            continue;
        }
        if p.action.trim().eq_ignore_ascii_case("read") &&
            let Some(s) = p.scope.get("subject").and_then(|v| v.as_str())
        {
            let s = s.trim();
            if !s.is_empty() {
                if !out.is_empty() {
                    out.push(',');
                }
                out.push_str(s);
            }
        }
    }

    if out.is_empty() { None } else { Some(out) }
}

fn build_tagging_query(
    tenant_id: &str,
    owner: &str,
    viewers_csv: Option<&str>,
) -> Option<String> {
    fn enc(s: &str) -> String {
        urlencoding::encode(s.trim()).into_owned()
    }

    let mut parts: Vec<String> = Vec::with_capacity(3);
    if !tenant_id.trim().is_empty() {
        parts.push(format!("tenant={}", enc(tenant_id)));
    }
    if !owner.trim().is_empty() {
        parts.push(format!("owner={}", enc(owner)));
    }
    if let Some(v) = viewers_csv.filter(|s| !s.trim().is_empty()) {
        parts.push(format!("viewer={}", enc(v)));
    }

    let q = parts.join("&");
    if q.is_empty() { None } else { Some(q) }
}
