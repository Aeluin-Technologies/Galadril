//! Scribe callbacks carry a capability, never browser-provided identity.

use std::sync::Arc;

use axum::Json;
use axum::extract::Extension;
use axum::http::{HeaderMap, StatusCode};
use serde::Deserialize;

#[derive(Deserialize)]
struct OAuthCallback {
    state: String,
    code: Option<String>,
    client_id: Option<String>,
    error: Option<String>,
}

/// A separate loopback port receives codes without forwarding browser
/// sessions.
pub fn chatgpt_callback_router(service: Arc<PengoloService>) -> axum::Router {
    axum::Router::new()
        .route("/auth/callback", axum::routing::get(chatgpt_callback))
        .layer(Extension(service))
}

async fn chatgpt_callback(
    Extension(service): Extension<Arc<PengoloService>>,
    axum::extract::Query(callback): axum::extract::Query<OAuthCallback>,
) -> impl axum::response::IntoResponse {
    let valid = (32..=256).contains(&callback.state.len()) &&
        callback.code.as_ref().is_none_or(|code| code.len() <= 8192) &&
        callback.client_id.as_ref().is_none_or(|id| id.len() <= 256) &&
        callback
            .error
            .as_ref()
            .is_none_or(|error| error.len() <= 256);
    let completed = if valid {
        service.complete_openai_account(serde_json::json!({"operation":"callback","state":callback.state,"code":callback.code,"client_id":callback.client_id,"error":callback.error})).await.is_ok()
    } else {
        false
    };
    tracing::info!(event.name = "chatgpt.oauth.callback", completed);
    (
        if completed {
            StatusCode::OK
        } else {
            StatusCode::BAD_REQUEST
        },
        [
            ("cache-control", "no-store"),
            ("referrer-policy", "no-referrer"),
            (
                "content-security-policy",
                "default-src 'none'; frame-ancestors 'none'",
            ),
        ],
        if completed {
            "Authorization completed. Return to Galadril to check your ChatGPT connection."
        } else {
            "The ChatGPT connection could not complete. Return to Galadril and start a new connection."
        },
    )
}

use crate::application::usecases::pengolo::PengoloService;
use crate::domain::pengolo::OutputEvent;

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct GenerationOutput {
    generation_id: String,
    sequence: i32,
    kind: String,
    content: String,
}

pub async fn events(
    Extension(service): Extension<Arc<PengoloService>>,
    headers: HeaderMap,
    Json(output): Json<GenerationOutput>,
) -> Result<Json<serde_json::Value>, (StatusCode, &'static str)> {
    let token = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .ok_or((StatusCode::UNAUTHORIZED, "Unauthorized"))?;
    if output.sequence < 0 || output.content.len() > 65536 {
        return Err((StatusCode::BAD_REQUEST, "Invalid generation event"));
    }
    service
        .accept_output(
            token,
            &output.generation_id,
            OutputEvent {
                sequence: output.sequence,
                kind: output.kind,
                content: output.content,
            },
        )
        .await
        .map(|()| Json(serde_json::json!({"active":true})))
        .map_err(|_| (StatusCode::FORBIDDEN, "Generation unavailable"))
}
