//! GraphQL schema definition mapping to application use cases with FGAC.

use std::pin::Pin;

use futures::Stream;
use juniper::{
    FieldError, FieldResult, RootNode, graphql_object, graphql_scalar,
    graphql_subscription,
};
use serde_json::Value;

use crate::adapters::inbound::graphql::context::AppContext;
use crate::application::usecases::authorization::QueryContext;
use crate::domain::permission::{Effect, IamPermission};

/// A custom GraphQL scalar to represent dynamic JSON objects.
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

pub struct Query;

#[graphql_object(context = AppContext)]
impl Query {
    /// Discovers all data tables the current user is authorized to see.
    async fn available_tables(
        #[graphql(context)] ctx: &AppContext,
    ) -> FieldResult<Vec<GqlSinkMetadata>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let tables = ctx
            .data_explorer
            .get_authorized_tables(&ctx.tenant_id, &ctx.user_id)
            .await?;

        let mut out = Vec::with_capacity(tables.len());
        for t in tables {
            out.push(GqlSinkMetadata {
                name: t.name,
                columns: t.columns,
            });
        }
        Ok(out)
    }

    /// Queries a specific table dynamically, applying RLS + Cedar.
    async fn query_table(
        #[graphql(context)] ctx: &AppContext,
        table_name: String,
        limit: Option<i32>,
        filters: Option<GqlQueryFilters>,
    ) -> FieldResult<Vec<DynamicJson>> {
        ctx.identity
            .verify_user(&ctx.tenant_id, &ctx.user_id)
            .await
            .map_err(FieldError::from)?;

        let safe_limit = limit.unwrap_or(10).clamp(1, 1000) as usize;
        let query_context = filters.map(|f| QueryContext {
            entity_id: f.entity_id,
            modality: f.modality,
            state_type: f.state_type,
            gis_zone: f.gis_zone,
        });

        let rows = ctx
            .data_explorer
            .query_table(
                &ctx.tenant_id,
                &ctx.user_id,
                &table_name,
                safe_limit,
                query_context,
            )
            .await?;

        Ok(rows.into_iter().map(DynamicJson).collect())
    }

    /// Searches entities by `entity_states.metadata.name` and returns only
    /// results the caller is authorized to read.
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
            .await?; // role may already exist; we ignore error only if it is conflict in adapter.

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
