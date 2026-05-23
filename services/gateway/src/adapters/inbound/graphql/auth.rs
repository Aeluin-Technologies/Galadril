//! JWT authentication middleware and extractor for Axum.

use std::sync::Arc;

use axum::Json;
use axum::extract::{Extension, FromRequestParts};
use axum::http::StatusCode;
use axum::http::request::Parts;
use axum::response::{IntoResponse, Response};
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode};
use serde::{Deserialize, Serialize};
use tracing::warn;

use crate::config::AppConfig;

/// Claims extracted from the JWT.
#[derive(Debug, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub exp: usize,
    pub tenant_id: String,
    #[serde(default)]
    pub iss: Option<String>,
    #[serde(default)]
    pub aud: Option<String>,
}

/// Pre-built JWT validation runtime.
pub struct JwtRuntime {
    key: DecodingKey,
    validation: Validation,
}

impl JwtRuntime {
    pub fn from_config(cfg: &AppConfig) -> Result<Self, AuthError> {
        let pem = cfg
            .jwt
            .es256_public_key_pem
            .as_deref()
            .ok_or(AuthError::Misconfigured)?;

        let key = DecodingKey::from_ec_pem(pem.as_bytes())
            .map_err(|_| AuthError::Misconfigured)?;

        let mut validation = Validation::new(Algorithm::ES256);

        if let Some(aud) = cfg.jwt.audience.as_deref() {
            validation.set_audience(&[aud]);
        }
        if let Some(iss) = cfg.jwt.issuer.as_deref() {
            validation.set_issuer(&[iss]);
        }

        Ok(Self { key, validation })
    }

    fn decode_claims(&self, token: &str) -> Result<Claims, AuthError> {
        let token_data = decode::<Claims>(token, &self.key, &self.validation)
            .map_err(|err| {
                warn!(error = %err, "jwt_decode_failed");
                AuthError::InvalidToken
            })?;
        Ok(token_data.claims)
    }
}

/// Error returned when JWT validation fails.
#[derive(Debug)]
pub enum AuthError {
    MissingToken,
    InvalidToken,
    Misconfigured,
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let (status, error_message) = match self {
            AuthError::MissingToken => {
                (StatusCode::UNAUTHORIZED, "Missing authorization header.")
            },
            AuthError::InvalidToken => {
                (StatusCode::UNAUTHORIZED, "Invalid or expired token.")
            },
            AuthError::Misconfigured => (
                StatusCode::INTERNAL_SERVER_ERROR,
                "Server auth misconfigured.",
            ),
        };

        let body = Json(serde_json::json!({ "error": error_message }));
        (status, body).into_response()
    }
}

impl<S> FromRequestParts<S> for Claims
where
    S: Send + Sync,
{
    type Rejection = AuthError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> Result<Self, Self::Rejection> {
        let Extension(jwt): Extension<Arc<JwtRuntime>> =
            Extension::from_request_parts(parts, state)
                .await
                .map_err(|_| AuthError::Misconfigured)?;

        let auth_header = parts
            .headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .filter(|value| value.starts_with("Bearer "))
            .ok_or(AuthError::MissingToken)?;

        let token = auth_header.trim_start_matches("Bearer ");
        jwt.decode_claims(token)
    }
}
