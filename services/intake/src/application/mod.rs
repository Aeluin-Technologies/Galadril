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

    fn canonical_subject(
        value: &str,
        tenant: &str,
        owner_only: bool,
    ) -> Result<String> {
        let value = value.trim();
        if value.is_empty() || value.chars().any(char::is_whitespace) {
            anyhow::bail!("authorization subject is invalid");
        }
        let typed = if value.starts_with("user:") ||
            value.starts_with("role:") ||
            value.starts_with("group:")
        {
            value.to_owned()
        } else {
            format!("user:{value}")
        };
        let (subject, relation) = typed
            .split_once('#')
            .map_or((typed.as_str(), None), |(subject, relation)| {
                (subject, Some(relation))
            });
        let (subject_type, subject_id) = subject
            .split_once(':')
            .context("authorization subject type is required")?;
        if subject_id.is_empty() || (owner_only && subject_type != "user") {
            anyhow::bail!("authorization subject type is not allowed");
        }
        match subject_type {
            "user" if relation.is_none() => {},
            "role" | "group"
                if !owner_only &&
                    relation == Some("member") &&
                    subject_id.starts_with(&format!("{tenant}/")) => {},
            _ => anyhow::bail!("authorization subject relation is invalid"),
        }
        Ok(typed)
    }

    fn inject_authz(
        record: &mut Value,
        topic: &str,
        tenant: &str,
        viewers: &[String],
        owner: Option<&String>,
        authentication_provenance: Option<&String>,
        delegation_id: Option<&String>,
    ) -> Result<()> {
        let obj = record
            .as_object_mut()
            .ok_or_else(|| anyhow!("record is not a JSON object"))?;

        let id = obj
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| anyhow!("record missing 'id' field"))?;

        let resource = format!("raw:{tenant}/{topic}:{id}");

        let mut tuples = Vec::with_capacity(viewers.len() + 2);

        tuples.push(json!({
            "resource": resource,
            "relation": "parent",
            "subject": format!("tenant:{tenant}")
        }));

        for v in viewers {
            tuples.push(json!({
                "resource": resource,
                "relation": "reader",
                "subject": Self::canonical_subject(v, tenant, false)?
            }));
        }

        if let Some(o) = owner {
            tuples.push(json!({
                "resource": resource,
                "relation": "owner",
                "subject": Self::canonical_subject(o, tenant, true)?
            }));
        }

        obj.insert(
            "authz".to_string(),
            json!({
                "tenant": tenant,
                "tuples": tuples,
                "source_principal": "service:intake",
                "execution_identity": "service:intake",
                "initiating_actor": owner.map_or("unknown", String::as_str),
                "authentication_provenance": authentication_provenance,
                "delegation_id": delegation_id,
                "requested_permission": "materialize",
                "requested_resource": resource
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
            .require_trusted_ingestion(key)
            .context("Untrusted or invalid ingestion delegation")?
            .to_owned();
        tracing::info!(
            event.name = "security.context.accepted",
            tenant_id = tenant,
            actor_id = hints.owner.as_deref().unwrap_or("unknown"),
            delegation_id =
                hints.delegation_id.as_deref().unwrap_or("unknown"),
            permission = "ingest",
            resource_id = key,
            execution_identity = "service:intake",
            "accepted scoped ingestion delegation"
        );

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
                hints.authentication_provenance.as_ref(),
                hints.delegation_id.as_ref(),
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
