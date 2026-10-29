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
        .map_err(|error| {
            let status = callback_failure_status(&error);
            let stage = ["resolve", "authorize", "validate", "persist"]
                .into_iter()
                .find(|stage| {
                    let marker = format!("pengolo.callback.{stage}");
                    error.chain().any(|cause| cause.to_string() == marker)
                })
                .unwrap_or("unknown");
            let database_code = error
                .downcast_ref::<sqlx::Error>()
                .and_then(sqlx::Error::as_database_error)
                .and_then(|cause| cause.code())
                .filter(|code| {
                    code.len() == 5 &&
                        code.bytes()
                            .all(|byte| byte.is_ascii_alphanumeric())
                });
            tracing::warn!(
                event.name = "pengolo.callback.failed",
                stage,
                status_code = status.as_u16(),
                sqlstate = database_code.as_deref().unwrap_or("none")
            );
            (status, "Generation unavailable")
        })
}

/// Retries dependency failures without reclassifying denied authority as
/// availability.
fn callback_failure_status(error: &anyhow::Error) -> StatusCode {
    if let Some(database) = error.downcast_ref::<sqlx::Error>() {
        let retryable = match database {
            sqlx::Error::PoolTimedOut |
            sqlx::Error::PoolClosed |
            sqlx::Error::Io(_) => true,
            sqlx::Error::Database(cause) => {
                cause.code().is_some_and(|code| retryable_sqlstate(&code))
            },
            _ => false,
        };
        if retryable {
            return StatusCode::SERVICE_UNAVAILABLE;
        }
    }
    if error
        .downcast_ref::<reqwest::Error>()
        .is_some_and(|error| error.is_timeout() || error.is_connect())
    {
        return StatusCode::SERVICE_UNAVAILABLE;
    }
    StatusCode::FORBIDDEN
}

/// SQLSTATE classes distinguish transient storage faults from invalid or
/// unauthorized data.
fn retryable_sqlstate(code: &str) -> bool {
    code.starts_with("08") ||
        matches!(
            code,
            "40001" | "40P01" | "55P03" | "57P01" | "57P02" | "57P03"
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callback_dependency_failures_remain_retryable_without_granting_access()
    {
        for error in [
            sqlx::Error::PoolTimedOut,
            sqlx::Error::PoolClosed,
            sqlx::Error::Io(std::io::Error::from(
                std::io::ErrorKind::ConnectionReset,
            )),
        ] {
            let failure = anyhow::Error::from(error)
                .context("callback persistence failed");
            assert_eq!(
                callback_failure_status(&failure),
                StatusCode::SERVICE_UNAVAILABLE
            );
        }
        for message in [
            "Invalid generation capability",
            "Generation evidence is no longer available",
            "Generation scope changed",
            "Output replay conflict",
        ] {
            assert_eq!(
                callback_failure_status(&anyhow::anyhow!(message)),
                StatusCode::FORBIDDEN
            );
        }
    }

    #[test]
    fn retryable_database_states_exclude_integrity_and_permission_failures() {
        for code in ["40001", "40P01", "55P03", "08006", "57P01"] {
            assert!(retryable_sqlstate(code));
        }
        for code in ["23505", "23514", "42501", "42P01", "22001"] {
            assert!(!retryable_sqlstate(code));
        }
    }
}
