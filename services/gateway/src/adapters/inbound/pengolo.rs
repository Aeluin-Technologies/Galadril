//! Scribe callbacks carry a capability, never browser-provided identity.

use std::sync::Arc;

use axum::Json;
use axum::extract::Extension;
use axum::http::{HeaderMap, StatusCode};
use serde::Deserialize;

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
