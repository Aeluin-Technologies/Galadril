//! Background mail work rechecks the live canonical identity before effects.

use std::sync::Arc;

use axum::Json;
use axum::extract::Extension;
use axum::http::{HeaderMap, StatusCode};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::adapters::inbound::graphql::server::GatewayServices;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MailActor {
    tenant: String,
    user: String,
}

pub async fn authorize(
    Extension(services): Extension<Arc<GatewayServices>>,
    headers: HeaderMap,
    Json(actor): Json<MailActor>,
) -> Result<Json<Value>, (StatusCode, &'static str)> {
    let supplied = headers.get_all("x-utils-key").iter().collect::<Vec<_>>();
    let valid = supplied.len() == 1 &&
        supplied
            .first()
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                services
                    .utils
                    .as_ref()
                    .is_some_and(|client| client.authenticates(value))
            });
    if !valid || actor.tenant.len() > 64 || actor.user.len() > 256 {
        return Err((StatusCode::FORBIDDEN, "Mail identity unavailable"));
    }
    services
        .identity
        .verify_user(&actor.tenant, &actor.user)
        .await
        .map_err(|_| (StatusCode::FORBIDDEN, "Mail identity unavailable"))?;
    let permitted = services
        .authorization
        .is_authorized(
            &actor.user,
            &actor.tenant,
            crate::application::usecases::authorization::Permission::View,
            "tenant",
            &actor.tenant,
            None,
        )
        .await
        .map_err(|_| (StatusCode::FORBIDDEN, "Mail identity unavailable"))?;
    if !permitted {
        return Err((StatusCode::FORBIDDEN, "Mail identity unavailable"));
    }
    Ok(Json(json!({"active": true})))
}
