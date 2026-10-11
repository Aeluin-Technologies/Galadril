//! First-organization enrollment for authenticated provisioning automation.

use std::sync::Arc;

use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::{Extension, Json};
use secrecy::ExposeSecret;
use serde_json::json;

use crate::adapters::inbound::graphql::server::GatewayServices;
use crate::application::usecases::authorization::QueryContext;
use crate::application::usecases::sso_admin::{
    LocalProvisionRequest, ProvisionRequest,
};

/// Compares an opaque provisioning credential without data-dependent byte
/// exits.
fn authorized(
    headers: &HeaderMap,
    expected: Option<&secrecy::SecretString>,
) -> bool {
    let Some(expected) = expected else {
        return false;
    };
    let mut values = headers.get_all("x-galadril-provisioning-key").iter();
    let Some(value) = values.next() else {
        return false;
    };
    if values.next().is_some() {
        return false;
    }
    let Some(value) = value.to_str().ok() else {
        return false;
    };
    let expected = expected.expose_secret().as_bytes();
    value.len() == expected.len()
        && value
            .as_bytes()
            .iter()
            .zip(expected)
            .fold(0_u8, |difference, (left, right)| {
                difference | (left ^ right)
            })
            == 0
}

/// Keeps deployment enrollment separate from ordinary tenant administration.
pub async fn provision(
    Extension(services): Extension<Arc<GatewayServices>>,
    headers: HeaderMap,
    Json(request): Json<ProvisionRequest>,
) -> Response {
    if !authorized(&headers, services.provisioning_key.as_ref()) {
        tracing::warn!(
            event.name = "organization.provisioning.denied",
            "provisioning authentication rejected"
        );
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error":"Provisioning authentication required"})),
        )
            .into_response();
    }
    let Some(service) = &services.sso_admin else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match service.provision(&request, &QueryContext::default()).await {
        Ok(organization) => (
            StatusCode::CREATED,
            Json(json!({"organization":organization.tenant_id,"ready":true})),
        )
            .into_response(),
        Err(_) => {
            tracing::warn!(
                event.name = "organization.provisioning.failed",
                "organization enrollment failed"
            );
            let error = json!({
                "error": "Organization enrollment failed; retry the same request ID after correcting configuration"
            });
            (StatusCode::CONFLICT, Json(error)).into_response()
        },
    }
}

/// Local account bootstrap is opt-in and uses the deployment enrollment key.
pub async fn provision_local(
    Extension(services): Extension<Arc<GatewayServices>>,
    headers: HeaderMap,
    Json(request): Json<LocalProvisionRequest>,
) -> Response {
    if !services.local_provisioning
        || !authorized(&headers, services.provisioning_key.as_ref())
    {
        tracing::warn!(
            event.name = "organization.local_provisioning.denied",
            "local provisioning rejected"
        );
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let Some(service) = &services.sso_admin else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match service.provision_local(&request, &QueryContext::default()).await {
        Ok(organization) => (StatusCode::CREATED, Json(json!({
            "organization": organization.tenant_id, "owner": organization.owner_id, "ready": true,
        }))).into_response(),
        Err(_) => {
            tracing::warn!(event.name = "organization.local_provisioning.failed", "local provisioning failed");
            (StatusCode::CONFLICT, Json(json!({"error":"Local enrollment failed"}))).into_response()
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn provisioning_rejects_missing_wrong_and_duplicate_credentials()
    -> anyhow::Result<()> {
        let expected = secrecy::SecretString::from("provisioning-secret");
        let mut headers = HeaderMap::new();
        assert!(!authorized(&headers, Some(&expected)));
        headers.insert("x-galadril-provisioning-key", "wrong".parse()?);
        assert!(!authorized(&headers, Some(&expected)));
        headers.insert(
            "x-galadril-provisioning-key",
            "provisioning-secret".parse()?,
        );
        assert!(authorized(&headers, Some(&expected)));
        assert!(!authorized(&headers, None));
        headers.append(
            "x-galadril-provisioning-key",
            "provisioning-secret".parse()?,
        );
        assert!(!authorized(&headers, Some(&expected)));
        Ok(())
    }
}
