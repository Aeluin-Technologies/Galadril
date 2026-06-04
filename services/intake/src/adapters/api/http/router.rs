//! Axum router for the intake HTTP API.

use std::sync::Arc;

use axum::Router;
use axum::extract::Extension;
use axum::routing::post;

use crate::adapters::api::http::handlers::upload_multipart;
use crate::domain::authz::AuthzService;
use crate::domain::jwt::JwtRuntime;
use crate::domain::ports::BlobStorage;

/// Creates the HTTP router for uploads.
pub fn create_router(
    jwt: Arc<JwtRuntime>,
    authz: Arc<AuthzService>,
    storage: Arc<dyn BlobStorage>,
) -> Router {
    Router::new()
        .route("/v1/intake/upload", post(upload_multipart))
        .layer(Extension(jwt))
        .layer(Extension(authz))
        .layer(Extension(storage))
}
