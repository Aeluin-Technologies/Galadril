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
use crate::adapters::outbound::database::iam::PgIamStore;
use crate::adapters::outbound::storage::s3::S3Uploader;
use crate::application::usecases::authorization::AuthService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::search::SearchService;
use crate::config::AppConfig;

/// Bootstraps the Axum router with pure GraphQL endpoints.
pub fn create_router(
    config: Arc<AppConfig>,
    jwt: Arc<JwtRuntime>,
    identity: Arc<IdentityService>,
    iam_admin: Arc<IamAdminService>,
    explore: Arc<ExploreService>,
    search: Arc<SearchService>,
    auth_service: Arc<AuthService>,
    iam_store: Arc<PgIamStore>,
    s3: Arc<S3Uploader>,
) -> Router {
    let schema = Arc::new(create_schema());

    Router::new()
        .route("/graphql", post(graphql_handler))
        .route("/graphql", get(graphql_ws))
        .layer(Extension(schema))
        .layer(Extension(config))
        .layer(Extension(identity))
        .layer(Extension(iam_admin))
        .layer(Extension(explore))
        .layer(Extension(search))
        .layer(Extension(auth_service))
        .layer(Extension(iam_store))
        .layer(Extension(s3))
        .layer(Extension(jwt))
}

/// Handles standard incoming GraphQL queries and mutations.
async fn graphql_handler(
    Extension(schema): Extension<Arc<AppSchema>>,
    Extension(config): Extension<Arc<AppConfig>>,
    Extension(identity): Extension<Arc<IdentityService>>,
    Extension(iam_admin): Extension<Arc<IamAdminService>>,
    Extension(explore): Extension<Arc<ExploreService>>,
    Extension(search): Extension<Arc<SearchService>>,
    Extension(auth_service): Extension<Arc<AuthService>>,
    Extension(iam_store): Extension<Arc<PgIamStore>>,
    Extension(s3): Extension<Arc<S3Uploader>>,
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
        auth_service,
        iam_store,
        s3,
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
    Extension(auth_service): Extension<Arc<AuthService>>,
    Extension(iam_store): Extension<Arc<PgIamStore>>,
    Extension(s3): Extension<Arc<S3Uploader>>,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    let context = AppContext {
        user_id: "ws_user".to_string(),
        tenant_id: "ws_tenant".to_string(),
        config,
        identity,
        iam_admin,
        explore,
        search,
        auth_service,
        iam_store,
        s3,
    };

    ws.on_upgrade(|socket| async move {
        let config = ConnectionConfig::new(context);
        subscriptions::serve_ws(socket, schema, config).await
    })
}
