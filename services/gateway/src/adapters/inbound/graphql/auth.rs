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
use crate::domain::validate_tenant_id;

/// Claims extracted from the JWT.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub exp: usize,
    pub tenant_id: String,
    #[serde(default)]
    pub iss: Option<String>,
    #[serde(default)]
    pub aud: Option<String>,
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub region: Option<String>,
    #[serde(default)]
    pub device_trust: Option<String>,
}

/// Pre-built JWT validation runtime.
pub struct JwtRuntime {
    key: DecodingKey,
    validation: Validation,
}

impl JwtRuntime {
    /// Builds strict ES256 validation with mandatory issuer and audience.
    pub fn from_config(cfg: &AppConfig) -> Result<Self, AuthError> {
        let pem = cfg
            .jwt
            .es256_public_key_pem
            .as_deref()
            .ok_or(AuthError::Misconfigured)?;

        let key = DecodingKey::from_ec_pem(pem.as_bytes())
            .map_err(|_| AuthError::Misconfigured)?;

        let audience = cfg
            .jwt
            .audience
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or(AuthError::Misconfigured)?;
        let issuer = cfg
            .jwt
            .issuer
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or(AuthError::Misconfigured)?;
        let mut validation = Validation::new(Algorithm::ES256);
        validation.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
        validation.set_audience(&[audience]);
        validation.set_issuer(&[issuer]);

        Ok(Self { key, validation })
    }

    /// Verifies a token and rejects empty or malformed principal scope.
    fn decode_claims(&self, token: &str) -> Result<Claims, AuthError> {
        let token_data = decode::<Claims>(token, &self.key, &self.validation)
            .map_err(|err| {
                warn!(error = %err, "jwt_decode_failed");
                AuthError::InvalidToken
            })?;
        let claims = token_data.claims;
        if claims.sub.trim().is_empty() ||
            validate_tenant_id(&claims.tenant_id).is_err()
        {
            return Err(AuthError::InvalidToken);
        }
        Ok(claims)
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
    /// Maps authentication failures without exposing verification internals.
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

    /// Extracts and verifies one bearer token for HTTP or WebSocket upgrade.
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

#[cfg(test)]
mod tests {
    use std::net::{IpAddr, Ipv4Addr};
    use std::time::{SystemTime, UNIX_EPOCH};

    use anyhow::{Context, Result};
    use jsonwebtoken::{EncodingKey, Header, encode};

    use super::*;
    use crate::config::{AuthConfig, DatabaseConfig, JwtConfig, ServerConfig};

    const PUBLIC_KEY: &str = r#"-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEEVs/o5+uQbTjL3chynL4wXgUg2R9
q9UU8I5mEovUf86QZ7kOBIjJwqnzD1omageEHWwHdBO6B+dFabmdT9POxg==
-----END PUBLIC KEY-----"#;
    const PRIVATE_KEY: &str = r#"-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgevZzL1gdAFr88hb2
OF/2NxApJCzGCEDdfSp6VQO30hyhRANCAAQRWz+jn65BtOMvdyHKcvjBeBSDZH2r
1RTwjmYSi9R/zpBnuQ4EiMnCqfMPWiZqB4QdbAd0E7oH50VpuZ1P087G
-----END PRIVATE KEY-----"#;

    fn config(issuer: Option<&str>, audience: Option<&str>) -> AppConfig {
        AppConfig {
            server: ServerConfig {
                host: IpAddr::V4(Ipv4Addr::LOCALHOST),
                port: 8080,
            },
            database: DatabaseConfig {
                host: "localhost".to_owned(),
                port: 5432,
                name: "test".to_owned(),
                username: "test".to_owned(),
                password: None,
                url: None,
            },
            jwt: JwtConfig {
                issuer: issuer.map(str::to_owned),
                audience: audience.map(str::to_owned),
                es256_public_key_pem: Some(PUBLIC_KEY.to_owned()),
                es256_private_key_pem: None,
            },
            auth: AuthConfig {
                spicedb_endpoint: None,
                spicedb_token: None,
                cedar_policy_dsl: String::new(),
            },
            s3: None,
        }
    }

    fn token(subject: &str, tenant_id: &str) -> Result<String> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .context("System clock is before the Unix epoch")?;
        let claims = Claims {
            sub: subject.to_owned(),
            exp: usize::try_from(now.as_secs().saturating_add(3600))?,
            tenant_id: tenant_id.to_owned(),
            iss: Some("https://issuer.example".to_owned()),
            aud: Some("galadril".to_owned()),
            role: None,
            region: None,
            device_trust: None,
        };
        let key = EncodingKey::from_ec_pem(PRIVATE_KEY.as_bytes())?;
        Ok(encode(&Header::new(Algorithm::ES256), &claims, &key)?)
    }

    #[test]
    fn jwt_runtime_requires_issuer_and_audience() {
        assert!(matches!(
            JwtRuntime::from_config(&config(None, Some("galadril"))),
            Err(AuthError::Misconfigured)
        ));
        assert!(matches!(
            JwtRuntime::from_config(&config(
                Some("https://issuer.example"),
                None
            )),
            Err(AuthError::Misconfigured)
        ));
    }

    #[test]
    fn signed_claims_require_valid_principal_and_tenant() -> Result<()> {
        let runtime = JwtRuntime::from_config(&config(
            Some("https://issuer.example"),
            Some("galadril"),
        ))
        .map_err(|error| anyhow::anyhow!("JWT runtime rejected: {error:?}"))?;
        let valid = runtime
            .decode_claims(&token("user-1", "tenant-1")?)
            .map_err(|error| {
                anyhow::anyhow!("valid token rejected: {error:?}")
            })?;
        assert_eq!(valid.sub, "user-1");
        assert_eq!(valid.tenant_id, "tenant-1");
        assert!(matches!(
            runtime.decode_claims(&token("", "tenant-1")?),
            Err(AuthError::InvalidToken)
        ));
        assert!(matches!(
            runtime.decode_claims(&token("user-1", "tenant-2/forged")?),
            Err(AuthError::InvalidToken)
        ));
        Ok(())
    }
}
