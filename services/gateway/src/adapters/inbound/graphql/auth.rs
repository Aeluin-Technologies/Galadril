//! Verified proxy identity extraction for the loopback-only application
//! listener.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::extract::FromRequestParts;
use axum::http::request::Parts;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::domain::validate_tenant_id;

/// Identity projected by Envoy after signature and issuer validation.
#[derive(Debug, Clone)]
pub struct Claims {
    pub sub: String,
    pub exp: usize,
    pub tenant_id: String,
    pub iss: Option<String>,
    pub role: Option<String>,
    pub region: Option<String>,
    pub device_trust: Option<String>,
}

impl Claims {
    /// Rejects ambiguous identity values before they reach tenant
    /// authorization.
    fn from_headers(headers: &HeaderMap, now: u64) -> Result<Self, AuthError> {
        let sub = required(headers, "x-galadril-sub")?;
        let tenant = required(headers, "x-galadril-tenant")?;
        let issuer = required(headers, "x-galadril-issuer")?;
        let exp = required(headers, "x-galadril-exp")?
            .parse::<usize>()
            .map_err(|_| AuthError::InvalidIdentity)?;
        if sub.trim().is_empty() ||
            validate_tenant_id(tenant).is_err() ||
            u64::try_from(exp).map_err(|_| AuthError::InvalidIdentity)? <=
                now
        {
            return Err(AuthError::InvalidIdentity);
        }
        Ok(Self {
            sub: sub.to_owned(),
            exp,
            tenant_id: tenant.to_owned(),
            iss: Some(issuer.to_owned()),
            role: optional(headers, "x-galadril-role")?.map(str::to_owned),
            region: optional(headers, "x-galadril-region")?.map(str::to_owned),
            device_trust: optional(headers, "x-galadril-device-trust")?
                .map(str::to_owned),
        })
    }
}

/// Fails closed on duplicate, empty or non-text claim values.
fn optional<'a>(
    headers: &'a HeaderMap,
    name: &str,
) -> Result<Option<&'a str>, AuthError> {
    let mut values = headers.get_all(name).iter();
    let Some(value) = values.next() else {
        return Ok(None);
    };
    if values.next().is_some() {
        return Err(AuthError::InvalidIdentity);
    }
    let text = value.to_str().map_err(|_| AuthError::InvalidIdentity)?;
    if text.trim().is_empty() {
        return Err(AuthError::InvalidIdentity);
    }
    Ok(Some(text))
}

/// Requires the claims that bind a principal to one tenant and validity
/// period.
fn required<'a>(
    headers: &'a HeaderMap,
    name: &str,
) -> Result<&'a str, AuthError> {
    optional(headers, name)?.ok_or(AuthError::InvalidIdentity)
}

#[derive(Debug)]
pub enum AuthError {
    InvalidIdentity,
    ClockUnavailable,
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        tracing::warn!(event.name = "authentication.identity.rejected", reason = ?self,
            "proxy identity rejected");
        let status = match self {
            Self::InvalidIdentity => StatusCode::UNAUTHORIZED,
            Self::ClockUnavailable => StatusCode::SERVICE_UNAVAILABLE,
        };
        (
            status,
            Json(serde_json::json!({"error": "Authentication rejected"})),
        )
            .into_response()
    }
}

impl<S: Send + Sync> FromRequestParts<S> for Claims {
    type Rejection = AuthError;

    /// Applies the same trusted identity contract to HTTP and WebSocket
    /// upgrades.
    async fn from_request_parts(
        parts: &mut Parts,
        _state: &S,
    ) -> Result<Self, Self::Rejection> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| AuthError::ClockUnavailable)?
            .as_secs();
        Self::from_headers(&parts.headers, now)
    }
}

#[cfg(test)]
mod tests {
    use anyhow::Result;

    use super::*;
    #[test]
    fn proxy_identity_requires_complete_unique_claims() -> Result<()> {
        use axum::http::{HeaderMap, HeaderValue};
        let mut headers = HeaderMap::new();
        headers.insert(
            "authorization",
            HeaderValue::from_static("Bearer forged"),
        );
        assert!(Claims::from_headers(&headers, 100).is_err());
        headers.insert("x-galadril-sub", HeaderValue::from_static("user-1"));
        headers
            .insert("x-galadril-tenant", HeaderValue::from_static("tenant-1"));
        headers.insert("x-galadril-exp", HeaderValue::from_static("101"));
        headers.insert(
            "x-galadril-issuer",
            HeaderValue::from_static("https://issuer.example"),
        );
        let claims = Claims::from_headers(&headers, 100).map_err(|error| {
            anyhow::anyhow!("proxy identity rejected: {error:?}")
        })?;
        assert_eq!(claims.sub, "user-1");
        assert_eq!(claims.tenant_id, "tenant-1");
        assert!(claims.role.is_none());
        assert!(Claims::from_headers(&headers, 101).is_err());
        headers.append("x-galadril-sub", HeaderValue::from_static("attacker"));
        assert!(Claims::from_headers(&headers, 100).is_err());
        Ok(())
    }

    #[test]
    fn proxy_identity_rejects_invalid_tenant_and_optional_duplicates() {
        use axum::http::{HeaderMap, HeaderValue};
        let mut headers = HeaderMap::new();
        for (name, value) in [
            ("x-galadril-sub", "user-1"),
            ("x-galadril-tenant", "tenant/escape"),
            ("x-galadril-exp", "101"),
            ("x-galadril-issuer", "https://issuer.example"),
        ] {
            headers.insert(name, HeaderValue::from_static(value));
        }
        assert!(Claims::from_headers(&headers, 100).is_err());
        headers
            .insert("x-galadril-tenant", HeaderValue::from_static("tenant-1"));
        headers.append("x-galadril-role", HeaderValue::from_static("reader"));
        headers.append("x-galadril-role", HeaderValue::from_static("admin"));
        assert!(Claims::from_headers(&headers, 100).is_err());
    }
}
