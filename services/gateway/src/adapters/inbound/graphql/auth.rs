//! JWT authentication middleware and extractor for Axum.

use std::sync::OnceLock;

use axum::Json;
use axum::extract::FromRequestParts;
use axum::http::StatusCode;
use axum::http::request::Parts;
use axum::response::{IntoResponse, Response};
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode};
use serde::{Deserialize, Serialize};
use tracing::warn;

static DECODING_KEY: OnceLock<DecodingKey> = OnceLock::new();

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

/// Error returned when JWT validation fails.
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

fn decoding_key_from_env() -> Result<&'static DecodingKey, AuthError> {
    if let Some(key) = DECODING_KEY.get() {
        return Ok(key);
    }

    let pem = std::env::var("GALADRIL_JWT_ES256_PUBLIC_KEY_PEM")
        .map_err(|_| AuthError::Misconfigured)?;

    let key = DecodingKey::from_ec_pem(pem.as_bytes())
        .map_err(|_| AuthError::Misconfigured)?;

    let _ = DECODING_KEY.set(key);

    DECODING_KEY.get().ok_or(AuthError::Misconfigured)
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
        let auth_header = parts
            .headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .filter(|value| value.starts_with("Bearer "))
            .ok_or(AuthError::MissingToken)?;

        let token = auth_header.trim_start_matches("Bearer ");

        let mut validation = Validation::new(Algorithm::ES256);

        if let Ok(aud) = std::env::var("GALADRIL_JWT_AUDIENCE") {
            validation.set_audience(&[aud.as_str()]);
        }
        if let Ok(iss) = std::env::var("GALADRIL_JWT_ISSUER") {
            validation.set_issuer(&[iss.as_str()]);
        }

        let key = decoding_key_from_env()?;

        let token_data =
            decode::<Claims>(token, key, &validation).map_err(|err| {
                warn!(error = %err, "jwt_decode_failed");
                AuthError::InvalidToken
            })?;

        Ok(token_data.claims)
    }
}
