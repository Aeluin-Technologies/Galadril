//! Galadril application logic.

pub mod parser;

use std::sync::Arc;

use anyhow::{Result, anyhow};
use async_trait::async_trait;
use regex::Regex;
use serde_json::{Value, json};

use crate::domain::models::PipelineConfig;
use crate::domain::ports::{BlobStorage, EventProducer, IngestionServicePort};

pub struct IngestionService {
    storage: Arc<dyn BlobStorage>,
    producer: Arc<dyn EventProducer>,
    pipeline_config: PipelineConfig,
}

impl IngestionService {
    /// Create a new [`IngestionService`].
    pub fn new(
        storage: Arc<dyn BlobStorage>,
        producer: Arc<dyn EventProducer>,
        pipeline_config: PipelineConfig,
    ) -> Self {
        Self {
            storage,
            producer,
            pipeline_config,
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
    async fn process(&self, bucket: String, key: String) -> Result<()> {
        let matched_source = self.pipeline_config.sources.iter().find(|s| {
            if let Some(pattern) = &s.match_pattern &&
                let Ok(re) = Regex::new(pattern)
            {
                return re.is_match(&key);
            }
            false
        });

        let source = match matched_source {
            Some(s) => s,
            None => {
                tracing::info!(
                    file = format!("s3://{bucket}/{key}"),
                    "ignoring unmapped file",
                );
                return Ok(());
            },
        };

        let hints = self.storage.authz_hints(&bucket, &key).await?;
        let tenant = hints
            .tenant
            .unwrap_or_else(|| Self::fallback_tenant_from_key(&key, &bucket));

        let content = if source.parser == "csv" || source.parser == "json" {
            self.storage.download_file(&key).await?
        } else {
            vec![]
        };

        let mut records =
            parser::parse_content(&source.parser, &content, &key, &bucket)?;

        for record in records.iter_mut() {
            Self::inject_authz(
                record,
                &source.topic,
                &tenant,
                &hints.viewers,
                hints.owner.as_ref(),
            )?;

            let routing_key = record
                .get("event_id")
                .or_else(|| record.get("image_id"))
                .or_else(|| record.get("document_id"))
                .or_else(|| record.get("article_id"))
                .and_then(|v| v.as_str())
                .unwrap_or(&key);

            self.producer
                .publish(
                    &source.topic,
                    source.schema_path.as_deref(),
                    routing_key,
                    record,
                )
                .await?;
        }

        Ok(())
    }
}
