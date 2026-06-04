//! JWT validation runtime (ES256).

use std::sync::Arc;

use axum::extract::FromRequestParts;
use axum::http::request::Parts;
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode};
use serde::{Deserialize, Serialize};
use tracing::warn;

use crate::config::AppConfig;

/// Claims extracted from the JWT (same shape as gateway).
#[derive(Debug, Clone, Serialize, Deserialize)]
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

    /// Extracts Claims from the request Authorization header (Bearer token).
    pub async fn claims_from_request(&self) -> Result<Claims, AuthError> {
        Err(AuthError::Misconfigured)
    }
}

/// Error returned when JWT validation fails.
#[derive(Debug)]
pub enum AuthError {
    MissingToken,
    InvalidToken,
    Misconfigured,
}

impl axum::response::IntoResponse for AuthError {
    fn into_response(self) -> axum::response::Response {
        use axum::Json;
        use axum::http::StatusCode;

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
        _state: &S,
    ) -> Result<Self, Self::Rejection> {
        let ext = parts
            .extensions
            .get::<Arc<JwtRuntime>>()
            .ok_or(AuthError::Misconfigured)?;

        let auth_header = parts
            .headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .filter(|value| value.starts_with("Bearer "))
            .ok_or(AuthError::MissingToken)?;

        let token = auth_header.trim_start_matches("Bearer ");
        ext.decode_claims(token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claims_deserialize_shape() {
        let raw = r#"{
          "sub":"user:alice",
          "exp":1710000000,
          "tenant_id":"t1",
          "iss":"issuer",
          "aud":"aud"
        }"#;

        let c: Claims = serde_json::from_str(raw).unwrap();
        assert_eq!(c.sub, "user:alice");
        assert_eq!(c.tenant_id, "t1");
    }
}
