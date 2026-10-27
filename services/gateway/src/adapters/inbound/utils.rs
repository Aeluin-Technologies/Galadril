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
    #[serde(default)]
    pipeline_id: Option<String>,
    #[serde(default)]
    revision_id: Option<String>,
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
    match (&actor.pipeline_id, &actor.revision_id) {
        (Some(pipeline), Some(revision)) => {
            use chrono::Timelike as _;
            let context =
                crate::application::usecases::authorization::QueryContext {
                    hour_utc: i64::from(chrono::Utc::now().hour()),
                    request_id: uuid::Uuid::new_v4().to_string(),
                    ..Default::default()
                };
            let definition = services
                .pipelines
                .read(&actor.tenant, &actor.user, &context, pipeline, None)
                .await
                .map_err(|_| {
                    (StatusCode::FORBIDDEN, "Mail pipeline unavailable")
                })?;
            if !definition.is_some_and(|definition|definition.published_revision_id.as_deref()==Some(revision)) || !services.authorization.is_authorized(&actor.user,&actor.tenant,crate::application::usecases::authorization::Permission::Execute,"pipeline",pipeline,Some(&context)).await.map_err(|_|(StatusCode::FORBIDDEN,"Mail pipeline unavailable"))? {return Err((StatusCode::FORBIDDEN,"Mail pipeline unavailable"));}
        },
        (None, None) => {},
        _ => return Err((StatusCode::FORBIDDEN, "Mail pipeline unavailable")),
    }
    Ok(Json(json!({"active": true})))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IngestionRequest {
    tenant: String,
    user: String,
    operation: IngestionOperation,
    group: String,
    staging: Option<String>,
    name: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IngestionOperation {
    Stage,
    Complete,
}

/// Reuses public upload policy, ownership, audit and finalization for
/// background inputs.
pub async fn ingest(
    Extension(services): Extension<Arc<GatewayServices>>,
    headers: HeaderMap,
    Json(input): Json<IngestionRequest>,
) -> Result<Json<Value>, (StatusCode, &'static str)> {
    use chrono::Timelike as _;

    use crate::application::usecases::authorization::QueryContext;
    use crate::application::usecases::uploads::UploadDestination;
    drop(
        authorize(
            Extension(Arc::clone(&services)),
            headers,
            Json(MailActor {
                tenant: input.tenant.clone(),
                user: input.user.clone(),
                pipeline_id: None,
                revision_id: None,
            }),
        )
        .await?,
    );
    if input.group != "email" {
        return Err((StatusCode::FORBIDDEN, "Mail ingestion unavailable"));
    }
    let context = QueryContext {
        hour_utc: i64::from(chrono::Utc::now().hour()),
        request_id: uuid::Uuid::new_v4().to_string(),
        ..Default::default()
    };
    let result=match input.operation {
        IngestionOperation::Stage if input.staging.is_none() && input.name.is_none()=>services.uploads.request_staging_upload(&input.tenant,&input.user,&context,Some("email")).await.map(|staging|json!({"data":{"requestStagingUpload":{"uploadUrl":staging.upload_url,"stagingKey":staging.staging_key}}})),
        IngestionOperation::Complete=>match (input.staging.as_deref(),input.name.as_deref()) {
            (Some(staging),Some(name)) if staging.len()<=2048 && name.len()<=255=>services.uploads.complete_upload(&input.tenant,&input.user,"service:utils",&context,UploadDestination{staging_key:staging,target_name:name,group_id:Some("email")}).await.map(|key|json!({"data":{"completeUpload":key}})),
            _=>return Err((StatusCode::BAD_REQUEST,"Invalid mail ingestion request")),
        },
        _=>return Err((StatusCode::BAD_REQUEST,"Invalid mail ingestion request")),
    };
    result.map(Json).map_err(|_| {
        tracing::warn!(event.name = "utils.ingestion.refused");
        (StatusCode::FORBIDDEN, "Mail ingestion unavailable")
    })
}
