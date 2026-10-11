//! Service-authenticated GraphQL surface, excluded from public ingress routes.

use std::sync::Arc;

use axum::extract::DefaultBodyLimit;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Extension, Json, Router};
use juniper::http::GraphQLRequest;
use juniper::{EmptySubscription, FieldError, FieldResult, RootNode};
use secrecy::{ExposeSecret, SecretString};
use sqlx::postgres::PgPoolOptions;

use super::store::AuthStore;

pub struct Context {
    store: AuthStore,
}
impl juniper::Context for Context {}

pub struct Query;
pub struct Mutation;
type Schema = RootNode<Query, Mutation, EmptySubscription<Context>>;

fn rejected(_: impl std::fmt::Display) -> FieldError {
    tracing::warn!(
        event.name = "authentication.persistence.rejected",
        "authentication operation rejected"
    );
    FieldError::new(
        "Authentication unavailable or denied",
        juniper::Value::null(),
    )
}

#[juniper::graphql_object(context = Context)]
impl Query {
    async fn login_provider(
        context: &Context,
        email: String,
        local_enabled: bool,
    ) -> FieldResult<String> {
        let result = context
            .store
            .login_provider(&email, local_enabled)
            .await
            .map_err(rejected)?;
        serde_json::to_string(&result).map_err(rejected)
    }
    async fn active_user(
        context: &Context,
        tenant: String,
        user: String,
    ) -> FieldResult<String> {
        context
            .store
            .active_user(&tenant, &user)
            .await
            .map_err(rejected)
    }
    async fn passkey_context(
        context: &Context,
        tenant: String,
        user: String,
    ) -> FieldResult<String> {
        let result = context
            .store
            .passkey_context(&tenant, &user)
            .await
            .map_err(rejected)?;
        serde_json::to_string(&result).map_err(rejected)
    }
}

#[juniper::graphql_object(context = Context)]
impl Mutation {
    async fn resolve_identity(
        context: &Context,
        domain: String,
        connection: String,
        external: String,
        profile: String,
    ) -> FieldResult<String> {
        let profile = serde_json::from_str(&profile).map_err(rejected)?;
        context
            .store
            .resolve_identity(&domain, &connection, &external, &profile)
            .await
            .map_err(rejected)
    }
    async fn bind_local(
        context: &Context,
        tenant: String,
        user: String,
        external: String,
        profile: String,
    ) -> FieldResult<bool> {
        let profile = serde_json::from_str(&profile).map_err(rejected)?;
        context
            .store
            .bind_local(&tenant, &user, &external, &profile)
            .await
            .map_err(rejected)?;
        Ok(true)
    }
    async fn challenge(
        context: &Context,
        tenant: String,
        user: String,
        flow: String,
        value: String,
    ) -> FieldResult<bool> {
        let value = serde_json::from_str(&value).map_err(rejected)?;
        context
            .store
            .challenge(&tenant, &user, &flow, &value)
            .await
            .map_err(rejected)?;
        Ok(true)
    }
    async fn consume(
        context: &Context,
        tenant: String,
        user: String,
        flow: String,
    ) -> FieldResult<String> {
        let value = context
            .store
            .consume(&tenant, &user, &flow)
            .await
            .map_err(rejected)?;
        serde_json::to_string(&value).map_err(rejected)
    }
    async fn save_credential(
        context: &Context,
        tenant: String,
        user: String,
        generation: String,
        credential: String,
        previous_counter: Option<String>,
    ) -> FieldResult<bool> {
        let generation = generation.parse().map_err(rejected)?;
        let credential =
            serde_json::from_str(&credential).map_err(rejected)?;
        let previous = previous_counter
            .map(|value| value.parse())
            .transpose()
            .map_err(rejected)?;
        context
            .store
            .save(&tenant, &user, generation, &credential, previous)
            .await
            .map_err(rejected)?;
        Ok(true)
    }
}

struct State {
    context: Context,
    schema: Schema,
    key: SecretString,
}

fn authorized(headers: &HeaderMap, expected: &SecretString) -> bool {
    let mut values = headers.get_all("x-galadril-auth-key").iter();
    let Some(value) = values.next() else {
        return false;
    };
    if values.next().is_some() {
        return false;
    }
    let expected = expected.expose_secret().as_bytes();
    value.as_bytes().len() == expected.len()
        && value
            .as_bytes()
            .iter()
            .zip(expected)
            .fold(0_u8, |difference, (left, right)| {
                difference | (left ^ right)
            })
            == 0
}

async fn execute(
    Extension(state): Extension<Arc<State>>,
    headers: HeaderMap,
    Json(request): Json<GraphQLRequest>,
) -> Response {
    if !authorized(&headers, &state.key) {
        tracing::warn!(
            event.name = "authentication.persistence.unauthorized",
            "private GraphQL credential rejected"
        );
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let response = request.execute(&state.schema, &state.context).await;
    ([("cache-control", "no-store")], Json(response)).into_response()
}

/// Keeps the low-privilege authentication pool inside Gateway, never in Nuxt.
pub async fn router(
    database_url: &str,
    key: SecretString,
) -> anyhow::Result<Router> {
    anyhow::ensure!(
        key.expose_secret().len() >= 32,
        "Private authentication key requires 32 characters"
    );
    let pool = PgPoolOptions::new()
        .max_connections(10)
        .acquire_timeout(std::time::Duration::from_secs(3))
        .after_connect(|connection, _metadata| {
            Box::pin(async move {
                sqlx::query("SET statement_timeout = '5s'")
                    .execute(connection)
                    .await?;
                Ok(())
            })
        })
        .connect(database_url)
        .await?;
    let bypasses_rls: bool = sqlx::query_scalar("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
        .fetch_one(&pool).await?;
    anyhow::ensure!(
        !bypasses_rls,
        "Authentication database identity must enforce RLS"
    );
    let state = Arc::new(State {
        context: Context {
            store: AuthStore::new(pool),
        },
        schema: Schema::new(Query, Mutation, EmptySubscription::new()),
        key,
    });
    Ok(Router::new()
        .route("/internal/auth/graphql", post(execute))
        .layer(Extension(state))
        .layer(DefaultBodyLimit::max(32768)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_graphql_rejects_missing_wrong_and_duplicate_service_credentials()
    -> anyhow::Result<()> {
        let expected =
            SecretString::from("test-private-authentication-key-1234567890");
        let mut headers = HeaderMap::new();
        assert!(!authorized(&headers, &expected));
        headers.insert("x-galadril-auth-key", "wrong".parse()?);
        assert!(!authorized(&headers, &expected));
        headers
            .insert("x-galadril-auth-key", expected.expose_secret().parse()?);
        assert!(authorized(&headers, &expected));
        headers
            .append("x-galadril-auth-key", expected.expose_secret().parse()?);
        assert!(!authorized(&headers, &expected));
        Ok(())
    }

    #[test]
    fn provider_lookup_rejects_ambiguous_or_malformed_addresses_before_io() {
        for email in [
            "alice@@example.com",
            "alice@example.com\n",
            "@example.com",
            "alice@",
            "alice@localhost",
        ] {
            assert!(super::super::store::email_domain(email).is_err());
        }
        assert_eq!(
            super::super::store::email_domain("Alice@EXAMPLE.COM").ok(),
            Some("EXAMPLE.COM")
        );
    }
}
