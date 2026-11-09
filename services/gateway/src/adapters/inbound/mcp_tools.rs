//! Only the audience-bound MCP listener may project OAuth identity onto this
//! route.

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::extract::Extension;
use axum::http::{HeaderMap, StatusCode};
use chrono::Timelike as _;
use serde_json::Value;

use crate::application::usecases::authorization::QueryContext;
use crate::application::usecases::chat_tools::{ChatTools, WorkspaceIdentity};
use crate::domain::mcp_workspace::WorkspaceRequest;

type Failure = (StatusCode, &'static str);

fn header<'a>(headers: &'a HeaderMap, name: &str) -> Result<&'a str, Failure> {
    let mut values = headers.get_all(name).iter();
    let value = values
        .next()
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty())
        .ok_or((StatusCode::UNAUTHORIZED, "MCP identity required"))?;
    if values.next().is_some() {
        return Err((StatusCode::UNAUTHORIZED, "Ambiguous MCP identity"));
    }
    Ok(value)
}

/// MCP OAuth audiences never become ordinary GraphQL sessions.
pub async fn invoke(
    Extension(tools): Extension<Arc<ChatTools>>,
    headers: HeaderMap,
    Json(input): Json<WorkspaceRequest>,
) -> Result<Json<Value>, Failure> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| (StatusCode::SERVICE_UNAVAILABLE, "Clock unavailable"))?
        .as_secs();
    let claims = super::graphql::auth::Claims::from_headers(&headers, now)
        .map_err(|_| (StatusCode::UNAUTHORIZED, "MCP identity required"))?;
    let client = header(&headers, "x-galadril-mcp-client")?;
    let scopes = header(&headers, "x-galadril-mcp-scopes")?;
    if client.len() > 128 || scopes.len() > 4096 {
        return Err((StatusCode::UNAUTHORIZED, "Invalid MCP identity"));
    }
    let lifetime = u64::try_from(claims.exp)
        .map_err(|_| (StatusCode::UNAUTHORIZED, "Invalid expiry"))?
        .saturating_sub(now);
    let identity = WorkspaceIdentity {
        tenant: claims.tenant_id,
        user: claims.sub,
        client: client.into(),
        scopes: scopes.split_whitespace().map(str::to_owned).collect(),
        context: QueryContext {
            role: claims.role,
            region: claims.region,
            internal_device: claims.device_trust.as_deref() == Some("managed"),
            hour_utc: i64::from(chrono::Utc::now().hour()),
            ..QueryContext::default()
        },
        lifetime: Duration::from_secs(lifetime.min(900)),
    };
    tools
        .execute_workspace(identity, input)
        .await
        .map(Json)
        .map_err(denied)
}

pub async fn delegated(
    Extension(tools): Extension<Arc<ChatTools>>,
    headers: HeaderMap,
    Json(input): Json<WorkspaceRequest>,
) -> Result<Json<Value>, Failure> {
    let token = header(&headers, "authorization")?
        .strip_prefix("Bearer ")
        .ok_or((StatusCode::UNAUTHORIZED, "Delegation required"))?;
    tools
        .invoke_workspace_grant(token, input)
        .await
        .map(Json)
        .map_err(denied)
}

fn denied(_error: anyhow::Error) -> Failure {
    tracing::warn!(
        event.name = "mcp.workspace.denied",
        "Workspace MCP operation unavailable"
    );
    (StatusCode::FORBIDDEN, "Workspace operation unavailable")
}
