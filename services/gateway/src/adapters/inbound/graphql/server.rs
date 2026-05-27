//! Axum HTTP and WebSocket server for GraphQL.

use std::sync::Arc;

use axum::Router;
use axum::extract::{Extension, WebSocketUpgrade};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use juniper_axum::extract::JuniperRequest;
use juniper_axum::response::JuniperResponse;
use juniper_axum::subscriptions;
use juniper_graphql_ws::ConnectionConfig;

use crate::adapters::inbound::graphql::auth::{Claims, JwtRuntime};
use crate::adapters::inbound::graphql::context::AppContext;
use crate::adapters::inbound::graphql::schema::{AppSchema, create_schema};
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::search::SearchService;
use crate::config::AppConfig;

/// Bootstraps the Axum router with GraphQL endpoints.
pub fn create_router(
    config: Arc<AppConfig>,
    jwt: Arc<JwtRuntime>,
    identity: Arc<IdentityService>,
    iam_admin: Arc<IamAdminService>,
    explore: Arc<ExploreService>,
    search: Arc<SearchService>,
) -> Router {
    let schema = Arc::new(create_schema());

    Router::new()
        .route("/graphql", post(graphql_handler).get(graphql_ws))
        .route(
            "/graphiql",
            get(juniper_axum::graphiql("/graphql", "/graphql")),
        )
        .route(
            "/playground",
            get(juniper_axum::playground("/graphql", "/graphql")),
        )
        .layer(Extension(schema))
        .layer(Extension(config))
        .layer(Extension(jwt))
        .layer(Extension(identity))
        .layer(Extension(iam_admin))
        .layer(Extension(explore))
        .layer(Extension(search))
}

/// Handles standard GraphQL POST requests.
async fn graphql_handler(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(config): Extension<Arc<AppConfig>>,
    Extension(identity): Extension<Arc<IdentityService>>,
    Extension(iam_admin): Extension<Arc<IamAdminService>>,
    Extension(explore): Extension<Arc<ExploreService>>,
    Extension(search): Extension<Arc<SearchService>>,
    claims: Claims,
    JuniperRequest(req): JuniperRequest,
) -> JuniperResponse {
    let context = AppContext {
        user_id: claims.sub,
        tenant_id: claims.tenant_id,
        config,
        identity,
        iam_admin,
        explore,
        search,
    };

    let response = req.execute(&*schema, &context).await;
    JuniperResponse(response)
}

/// Handles GraphQL WebSocket subscriptions.
async fn graphql_ws(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(config): Extension<Arc<AppConfig>>,
    Extension(identity): Extension<Arc<IdentityService>>,
    Extension(iam_admin): Extension<Arc<IamAdminService>>,
    Extension(explore): Extension<Arc<ExploreService>>,
    Extension(search): Extension<Arc<SearchService>>,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    // TODO: authenticate WS and build context from token.
    let context = AppContext {
        user_id: "ws_user".to_string(),
        tenant_id: "ws_tenant".to_string(),
        config,
        identity,
        iam_admin,
        explore,
        search,
    };

    ws.on_upgrade(|socket| async move {
        let config = ConnectionConfig::new(context);
        subscriptions::serve_graphql_ws(socket, schema, config).await;
    })
}
