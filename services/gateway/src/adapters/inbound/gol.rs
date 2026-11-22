//! The private worker channel carries opaque admissions, never caller
//! clearance.

mod wire;

use std::sync::Arc;

use axum::Json;
use axum::body::Body;
use axum::extract::Extension;
use axum::http::{Request, StatusCode, header};
use axum::response::{IntoResponse, Response};
pub(crate) use wire::enforce_content_length;

use crate::adapters::inbound::graphql::server::GatewayServices;
use crate::application::usecases::gol::runtime::{
    EffectRequest, RuntimeError, RuntimeOpenRequest,
};

/// Authenticates normal opens and explicit re-admission from signed lineage.
#[tracing::instrument(name = "gol.http.runtime", skip_all)]
pub async fn open(
    Extension(services): Extension<Arc<GatewayServices>>,
    request: Request<Body>,
) -> Response {
    let request =
        match wire::decode::<RuntimeOpenRequest>(request, 72 * 1024, |key| {
            services.gol.verify_worker_key(key)
        })
        .await
        {
            Ok(request) => request,
            Err(response) => return response,
        };
    match request {
        RuntimeOpenRequest::Open(request) => {
            match services.gol.runtime_open(request).await {
                Ok(lease) => {
                    ([(header::CACHE_CONTROL, "no-store")], Json(lease))
                        .into_response()
                },
                Err(error) => failure(error),
            }
        },
        RuntimeOpenRequest::Resume(request) => {
            match services.gol.runtime_resume(request).await {
                Ok(admission) => {
                    ([(header::CACHE_CONTROL, "no-store")], Json(admission))
                        .into_response()
                },
                Err(error) => failure(error),
            }
        },
    }
}

/// The host validates the exact value and retained dependencies before
/// release.
#[tracing::instrument(name = "gol.http.effect", skip_all)]
pub async fn effect(
    Extension(services): Extension<Arc<GatewayServices>>,
    request: Request<Body>,
) -> Response {
    let request =
        match wire::decode::<EffectRequest>(request, 160 * 1024, |key| {
            services.gol.verify_worker_key(key)
        })
        .await
        {
            Ok(request) => request,
            Err(response) => return response,
        };
    match services.gol.runtime_effect(request).await {
        Ok(decision) => {
            ([(header::CACHE_CONTROL, "no-store")], Json(decision))
                .into_response()
        },
        Err(error) => failure(error),
    }
}

fn failure(error: RuntimeError) -> Response {
    let status = match error {
        RuntimeError::Invalid => StatusCode::BAD_REQUEST,
        RuntimeError::Denied => StatusCode::FORBIDDEN,
        RuntimeError::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
    };
    tracing::warn!(status = status.as_u16(), "GOL worker request unavailable");
    wire::failure(status)
}
