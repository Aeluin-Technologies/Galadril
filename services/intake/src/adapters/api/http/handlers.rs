//! HTTP handlers for multipart upload ingestion.

use std::sync::Arc;

use axum::Json;
use axum::extract::{Extension, Multipart};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::Serialize;

use crate::domain::authz::{AuthzService, Permission};
use crate::domain::jwt::{AuthError, Claims, JwtRuntime};
use crate::domain::ports::{AuthzHints, BlobStorage};
use crate::domain::upload_key::{SanitizedUpload, sanitize_upload_request};

/// JSON response for accepted uploads.
#[derive(Debug, Serialize)]
struct UploadAccepted {
    bucket: String,
    key: String,
    s3_url: String,
}

#[derive(Debug)]
pub enum UploadError {
    Auth(AuthError),
    BadRequest(&'static str),
    Forbidden,
    Storage(anyhow::Error),
    Internal(anyhow::Error),
}

impl IntoResponse for UploadError {
    fn into_response(self) -> Response {
        match self {
            UploadError::Auth(e) => e.into_response(),
            UploadError::BadRequest(msg) => (
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({ "error": msg })),
            )
                .into_response(),
            UploadError::Forbidden => (
                StatusCode::FORBIDDEN,
                Json(serde_json::json!({ "error": "Forbidden." })),
            )
                .into_response(),
            UploadError::Storage(e) => {
                tracing::warn!(error = %e, "upload_storage_failed");
                (
                    StatusCode::BAD_GATEWAY,
                    Json(serde_json::json!({ "error": "Upstream storage failed." })),
                )
                    .into_response()
            },
            UploadError::Internal(e) => {
                tracing::warn!(error = %e, "upload_internal_error");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(serde_json::json!({ "error": "Internal error." })),
                )
                    .into_response()
            },
        }
    }
}

/// POST /v1/intake/upload
///
/// multipart/form-data fields:
/// - file: required
/// - name: required (object name)
/// - group_id: optional (defaults to "default")
/// - viewers: optional (comma-separated or repeated fields)
pub async fn upload_multipart(
    Extension(jwt): Extension<Arc<JwtRuntime>>,
    Extension(authz): Extension<Arc<AuthzService>>,
    Extension(storage): Extension<Arc<dyn BlobStorage>>,
    mut multipart: Multipart,
) -> Result<impl IntoResponse, UploadError> {
    let mut name: Option<String> = None;
    let mut group_id: Option<String> = None;
    let mut viewers: Vec<String> = Vec::new();
    let mut file_bytes: Option<Vec<u8>> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| UploadError::BadRequest("Invalid multipart payload."))?
    {
        let field_name = field.name().unwrap_or_default();

        match field_name {
            "name" => {
                let v = field.text().await.map_err(|_| {
                    UploadError::BadRequest("Invalid 'name' field.")
                })?;
                name = Some(v);
            },
            "group_id" => {
                let v = field.text().await.map_err(|_| {
                    UploadError::BadRequest("Invalid 'group_id' field.")
                })?;
                group_id = Some(v);
            },
            "viewers" | "viewer" => {
                let v = field.text().await.map_err(|_| {
                    UploadError::BadRequest("Invalid 'viewers' field.")
                })?;
                viewers.extend(
                    v.split(',')
                        .map(|x| x.trim())
                        .filter(|x| !x.is_empty())
                        .map(|x| x.to_string()),
                );
            },
            "file" => {
                // Axum multipart currently buffers into owned bytes. For large
                // files, switch to streaming multipart into S3 multipart
                // upload.
                let bytes = field.bytes().await.map_err(|_| {
                    UploadError::BadRequest("Invalid file upload.")
                })?;
                file_bytes = Some(bytes.to_vec());
            },
            _ => {
                // Ignore unknown fields for forward compatibility.
            },
        }
    }

    let name = name.ok_or(UploadError::BadRequest("Missing 'name' field."))?;
    let file_bytes =
        file_bytes.ok_or(UploadError::BadRequest("Missing 'file' field."))?;

    let Claims { sub, tenant_id, .. } =
        jwt.claims_from_request().await.map_err(UploadError::Auth)?;

    let sanitized: SanitizedUpload =
        sanitize_upload_request(&tenant_id, group_id.as_deref(), &name)
            .map_err(|_| {
                UploadError::BadRequest("Invalid upload key components.")
            })?;

    let allowed = authz
        .is_authorized(
            &sub,
            Permission::Write,
            "bucket",
            &sanitized.tenant_bucket_resource_id(),
        )
        .await
        .map_err(UploadError::Internal)?;

    if !allowed {
        return Err(UploadError::Forbidden);
    }

    let hints = AuthzHints {
        tenant: Some(format!("tenant:{}", sanitized.tenant_id)),
        viewers,
        owner: Some(sub.clone()),
    };

    let s3_url = storage
        .upload_file_with_authz(&sanitized.s3_key, &file_bytes, &hints)
        .await
        .map_err(UploadError::Storage)?;

    let body = UploadAccepted {
        bucket: sanitized.tenant_id.clone(),
        key: sanitized.s3_key.clone(),
        s3_url,
    };

    Ok((StatusCode::ACCEPTED, Json(body)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upload_accepted_serializes() {
        let a = UploadAccepted {
            bucket: "t1".to_string(),
            key: "t1/default/file.bin".to_string(),
            s3_url: "s3://b/t1/default/file.bin".to_string(),
        };
        let v = serde_json::to_value(a).unwrap();
        assert!(v.get("bucket").is_some());
        assert!(v.get("key").is_some());
        assert!(v.get("s3_url").is_some());
    }
}
