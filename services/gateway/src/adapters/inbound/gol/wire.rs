//! Authenticate the worker channel before allocating or interpreting its body.

use axum::body::{Body, to_bytes};
use axum::extract::DefaultBodyLimit;
use axum::http::{Method, Request, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use serde::de::DeserializeOwned;

const WORKER_HEADER: &str = "x-galadril-gol-worker-key";

/// A job handle alone never establishes the trusted dispatcher identity.
pub(crate) async fn decode<T: DeserializeOwned>(
    request: Request<Body>,
    limit: usize,
    verify: impl FnOnce(&str) -> bool,
) -> Result<T, Response> {
    let mut keys = request.headers().get_all(WORKER_HEADER).iter();
    let key =
        keys.next()
            .and_then(|value| value.to_str().ok())
            .filter(|value| {
                value.len() == 64 &&
                    value.bytes().all(|byte| byte.is_ascii_hexdigit())
            });
    if keys.next().is_some() || !key.is_some_and(verify) {
        return Err(failure(StatusCode::FORBIDDEN));
    }
    let mut content_types =
        request.headers().get_all(header::CONTENT_TYPE).iter();
    let valid_content_type = content_types
        .next()
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| {
            value.trim().eq_ignore_ascii_case("application/json")
        });
    if !valid_content_type || content_types.next().is_some() {
        return Err(failure(StatusCode::BAD_REQUEST));
    }
    let bytes = to_bytes(request.into_body(), limit)
        .await
        .map_err(|_| failure(StatusCode::BAD_REQUEST))?;
    serde_json::from_slice(&bytes)
        .map_err(|_| failure(StatusCode::BAD_REQUEST))
}

/// Denial responses never reveal policy, target or parser implementation
/// details.
pub(crate) fn failure(status: StatusCode) -> Response {
    (
        status,
        [(header::CACHE_CONTROL, "no-store")],
        "GOL operation unavailable",
    )
        .into_response()
}

/// Preserves public request limits while private decoders own worker
/// authentication.
pub(crate) async fn enforce_content_length(
    mut request: Request<Body>,
    next: Next,
    maximum: usize,
) -> Response {
    if request.method() == Method::POST &&
        matches!(
            request.uri().path(),
            "/internal/gol/runtime" | "/internal/gol/effect"
        )
    {
        return next.run(request).await;
    }
    let oversized = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
        .is_some_and(|length| length > maximum);
    if oversized {
        return (
            StatusCode::PAYLOAD_TOO_LARGE,
            "GraphQL request body exceeds the configured limit",
        )
            .into_response();
    }
    DefaultBodyLimit::max(maximum).apply(&mut request);
    next.run(request).await
}
