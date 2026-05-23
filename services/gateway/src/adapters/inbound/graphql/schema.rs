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

    fn columns(&self) -> &Vec<String> {
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

        Ok(tables
            .into_iter()
            .map(|t| GqlSinkMetadata {
                name: t.name,
                columns: t.columns,
            })
            .collect())
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

pub type AppSchema =
    RootNode<Query, juniper::EmptyMutation<AppContext>, Subscription>;

pub fn create_schema() -> AppSchema {
    AppSchema::new(Query, juniper::EmptyMutation::new(), Subscription)
}
