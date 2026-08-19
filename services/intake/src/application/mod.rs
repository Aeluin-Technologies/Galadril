//! Galadril application logic linking multi-tenant routing, transformation and
//! streaming.

pub mod layers;
pub mod parser;
pub mod pipeline;
pub mod router;

use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use async_trait::async_trait;
use serde_json::{Value, json};

use crate::application::router::PipelineRouter;
use crate::domain::models::FileEvent;
use crate::domain::ports::{BlobStorage, EventProducer, IngestionServicePort};

/// Standard event processor wrapping I/O bridges and dynamic tenant routing.
pub struct IngestionService {
    storage: Arc<dyn BlobStorage>,
    producer: Arc<dyn EventProducer>,
    router: Arc<PipelineRouter>,
}

impl IngestionService {
    /// Create a new [`IngestionService`].
    pub fn new(
        storage: Arc<dyn BlobStorage>,
        producer: Arc<dyn EventProducer>,
        router: Arc<PipelineRouter>,
    ) -> Self {
        Self {
            storage,
            producer,
            router,
        }
    }

    fn fallback_tenant_from_key(key: &str, bucket: &str) -> String {
        key.split('/').next().unwrap_or(bucket).to_string()
    }

    fn inject_authz(
        record: &mut Value,
        topic: &str,
        tenant: &str,
        viewers: &[String],
        owner: Option<&String>,
    ) -> Result<()> {
        let obj = record
            .as_object_mut()
            .ok_or_else(|| anyhow!("record is not a JSON object"))?;

        if obj.contains_key("authz") {
            return Ok(());
        }

        let id = obj
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| anyhow!("record missing 'id' field"))?;

        let resource = format!("raw:{topic}:{id}");

        let mut tuples = Vec::with_capacity(viewers.len() + 2);

        tuples.push(json!({
            "resource": resource,
            "relation": "tenant",
            "subject": tenant
        }));

        for v in viewers {
            tuples.push(json!({
                "resource": resource,
                "relation": "viewer",
                "subject": v
            }));
        }

        if let Some(o) = owner {
            tuples.push(json!({
                "resource": resource,
                "relation": "owner",
                "subject": o
            }));
        }

        obj.insert(
            "authz".to_string(),
            json!({
                "tenant": tenant,
                "tuples": tuples,
                "source_principal": "service:intake"
            }),
        );

        Ok(())
    }
}

#[async_trait]
impl IngestionServicePort for IngestionService {
    #[tracing::instrument(
        name = "intake.process_object",
        skip(self),
        fields(
            storage.bucket = %event.bucket,
            storage.key = %event.key,
            pipeline = "intake",
            step = "validate_and_route",
            entity_id = %event.key,
            trace_id = tracing::field::Empty,
            span_id = tracing::field::Empty,
        )
    )]
    async fn process(&self, event: FileEvent) -> Result<()> {
        crate::telemetry::record_current_trace_identifiers();
        let trace = crate::telemetry::current_trace_metadata();
        let bucket = &event.bucket;
        let key = &event.key;
        let hints = self.storage.authz_hints(bucket, key).await.with_context(
            || {
                format!(
                    "Failed to retrieve authz hints for s3://{bucket}/{key}"
                )
            },
        )?;

        let tenant = hints
            .tenant
            .clone()
            .unwrap_or_else(|| Self::fallback_tenant_from_key(key, bucket));

        let route = match self.router.resolve_route(&tenant, key).await {
            Ok(r) => r,
            Err(err) => {
                tracing::warn!(
                    event.name = "pipeline.route.rejected",
                    ?err,
                    tenant = %tenant,
                    file = format!("s3://{bucket}/{key}"),
                    "routing rejected or unmapped object path"
                );
                return Err(err);
            },
        };

        let ids = layers::observation_ids(&event);
        tracing::info!(
            event.name = "intake.source.preserved",
            ingestion_id = %ids.ingestion_id,
            storage.uri = format!("s3://{bucket}/{key}"),
            "raw object retained as the ingestion source"
        );

        let content = if parser::requires_content(&route.parser) {
            self.storage.download_file(key).await.with_context(|| {
                format!("Data payload missing or inaccessible for {key}")
            })?
        } else {
            vec![]
        };

        let mut records = parser::parse_content(
            &route.parser,
            &content,
            &parser::ParseContext {
                key,
                bucket,
                media_type: &event.content_type,
            },
        )
        .with_context(|| {
            format!("Parser '{}' failed on resource {key}", route.parser)
        })?;

        for (ordinal, record) in records.iter_mut().enumerate() {
            let observation_id = layers::enrich_record(
                record, &event, &route, &ids, &trace, ordinal,
            )
            .with_context(|| {
                format!("Observation validation failed for {key}")
            })?;
            Self::inject_authz(
                record,
                &route.topic,
                &tenant,
                &hints.viewers,
                hints.owner.as_ref(),
            ).context("Failed to inject cryptographic or structural authz tuple contexts")?;

            self.producer
                .publish(
                    &route.topic,
                    route.schema_path.as_deref(),
                    &observation_id,
                    record,
                )
                .await
                .with_context(|| {
                    format!(
                        "Broker publication failure on destination topic: {}",
                        route.topic
                    )
                })?;
        }

        tracing::info!(
            event.name = "intake.records.published",
            ingestion_id = %ids.ingestion_id,
            records = records.len(),
            topic = %route.topic,
            "validated observations published"
        );

        Ok(())
    }
}
