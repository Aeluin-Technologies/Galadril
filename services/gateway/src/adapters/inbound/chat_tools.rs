//! Private tool transport with authority supplied only by a scoped capability.

use std::sync::Arc;

use axum::Json;
use axum::extract::Extension;
use axum::http::{HeaderMap, StatusCode};
use serde_json::Value;

use crate::application::usecases::chat_tools::{ChatTools, ToolRequest};

pub async fn invoke(
    Extension(tools): Extension<Arc<ChatTools>>,
    headers: HeaderMap,
    Json(request): Json<ToolRequest>,
) -> Result<Json<Value>, (StatusCode, &'static str)> {
    let token = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .ok_or((StatusCode::UNAUTHORIZED, "Unauthorized"))?;
    tools.execute(token, request).await.map(Json).map_err(|_| {
        tracing::warn!(
            event.name = "chat.tool.denied",
            "Chat tool access denied or unavailable"
        );
        (StatusCode::FORBIDDEN, "Tool unavailable")
    })
}
