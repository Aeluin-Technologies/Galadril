//! Galadril application logic.

pub mod parser;

use std::sync::Arc;

use anyhow::Result;
use async_trait::async_trait;
use regex::Regex;

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
}

#[async_trait]
impl IngestionServicePort for IngestionService {
    async fn process(&self, bucket: String, key: String) -> Result<()> {
        // Find matching source config using regex on S3 key.
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
                    "Ignoring unmapped file: s3://{}/{}",
                    bucket,
                    key
                );
                return Ok(());
            },
        };

        let content = if source.parser == "csv" || source.parser == "json" {
            self.storage.download_file(&key).await?
        } else {
            vec![]
        };

        let records =
            parser::parse_content(&source.parser, &content, &key, &bucket)?;

        for record in records {
            // Deduce the best routing key.
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
                    &record,
                )
                .await?;
        }

        Ok(())
    }
}
