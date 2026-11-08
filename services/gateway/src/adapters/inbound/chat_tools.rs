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
    tools
        .execute(token, request)
        .await
        .map(Json)
        .map_err(|error| {
            tracing::warn!(
                event.name = "chat.tool.denied",
                failure_kind = denial_reason(&error),
                "Chat tool access denied or unavailable"
            );
            (StatusCode::FORBIDDEN, "Tool unavailable")
        })
}

/// Distinguish operational failures without logging error text or private
/// resources.
fn denial_reason(error: &anyhow::Error) -> &'static str {
    for cause in error.chain() {
        if cause.is::<sqlx::Error>() {
            return "database";
        }
        let reason = match cause.to_string().as_str() {
            "Workspace revision conflict" => "revision_conflict",
            "State exceeds limit" => "state_limit",
            "Evidence archives are immutable" => "immutable_state",
            "Generation is no longer active" => "inactive_generation",
            "Generation scope changed" | "Recovery scope changed" => {
                "scope_changed"
            },
            "Generation evidence is no longer available" => {
                "evidence_unavailable"
            },
            "State requires generation ownership" | "State unavailable" => {
                "state_ownership"
            },
            "Authorization denied" => "authorization",
            _ => continue,
        };
        return reason;
    }
    "unclassified"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn denial_diagnostics_never_include_private_error_text() {
        assert_eq!(
            denial_reason(&anyhow::anyhow!("Workspace revision conflict")),
            "revision_conflict"
        );
        assert_eq!(
            denial_reason(&anyhow::anyhow!("State exceeds limit")),
            "state_limit"
        );
        assert_eq!(
            denial_reason(&anyhow::anyhow!("private source or credential")),
            "unclassified"
        );
    }
}
