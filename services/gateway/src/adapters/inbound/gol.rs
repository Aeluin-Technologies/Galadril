//! The private worker channel carries opaque admissions, never caller
//! clearance.

mod wire;

use std::sync::Arc;

use axum::Json;
use axum::body::Body;
use axum::extract::Extension;
use axum::http::{Request, StatusCode, header};
use axum::response::{IntoResponse, Response};
use galadril_telemetry::contract::{ErrorCode, emit_gol_worker_failed};
use uuid::Uuid;
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
#[tracing::instrument(
    name = "gol.http.effect",
    skip_all,
    fields(gol.failure.phase = tracing::field::Empty, otel.status_code = tracing::field::Empty)
)]
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
            Err(response) => {
                let span = tracing::Span::current();
                span.record("otel.status_code", "ERROR");
                span.record("gol.failure.phase", "worker_request");
                return response;
            },
        };
    match services.gol.runtime_effect(request).await {
        Ok(decision) => {
            ([(header::CACHE_CONTROL, "no-store")], Json(decision))
                .into_response()
        },
        Err(error) => {
            tracing::Span::current().record("otel.status_code", "ERROR");
            failure(error)
        },
    }
}

fn failure(error: RuntimeError) -> Response {
    let code = match error {
        RuntimeError::Invalid => ErrorCode::InvalidInput,
        RuntimeError::Denied => ErrorCode::AccessDenied,
        RuntimeError::Unavailable => ErrorCode::Unavailable,
    };
    let status = StatusCode::from_u16(code.http_status())
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let mut buffer = Uuid::encode_buffer();
    let occurrence: &str =
        Uuid::new_v4().hyphenated().encode_lower(&mut buffer);
    emit_gol_worker_failed(code, occurrence);
    (
        status,
        [
            (header::CACHE_CONTROL.as_str(), "no-store"),
            ("x-galadril-error-code", code.as_str()),
            ("x-galadril-error-occurrence-id", occurrence),
        ],
        "GOL operation unavailable",
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_failures_carry_safe_correlation_fields() -> anyhow::Result<()> {
        let response = failure(RuntimeError::Denied);
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            response.headers().get("x-galadril-error-code"),
            Some(&axum::http::HeaderValue::from_static("access_denied"))
        );
        let value = response
            .headers()
            .get("x-galadril-error-occurrence-id")
            .ok_or_else(|| {
            anyhow::anyhow!("missing failure occurrence")
        })?;
        assert_eq!(
            uuid::Uuid::parse_str(value.to_str()?)?.get_version(),
            Some(uuid::Version::Random)
        );
        Ok(())
    }
}
