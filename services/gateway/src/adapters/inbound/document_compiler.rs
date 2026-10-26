//! Mesh policies admit only the compiler workload to this private callback.
use std::sync::Arc;

use axum::Json;
use axum::extract::Extension;
use axum::http::{HeaderMap, StatusCode};
use galadril_document_compiler::delegation::{AuthorizationRequest, Grant};

use crate::application::usecases::chat_tools::ChatTools;
use crate::application::usecases::pengolo::PengoloService;

pub async fn authorize(
    Extension(service): Extension<Arc<PengoloService>>,
    Extension(tools): Extension<Arc<ChatTools>>,
    headers: HeaderMap,
    Json(request): Json<AuthorizationRequest>,
) -> Result<Json<Grant>, (StatusCode, &'static str)> {
    let mut headers = headers.get_all("authorization").iter();
    let token = headers
        .next()
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .ok_or((StatusCode::UNAUTHORIZED, "Delegation required"))?;
    if headers.next().is_some() || !tools.document_compiler_enabled() {
        return Err((StatusCode::FORBIDDEN, "Compiler unavailable"));
    }
    service
        .authorize_compiler(token, request)
        .await
        .map(Json)
        .map_err(|_| (StatusCode::FORBIDDEN, "Delegation denied"))
}
