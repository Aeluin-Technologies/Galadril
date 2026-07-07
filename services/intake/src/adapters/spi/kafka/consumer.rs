//! Kafka consumer for incoming bucket event handling.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow};
use rdkafka::Message;
use rdkafka::config::ClientConfig;
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use serde::Deserialize;

use crate::domain::ports::IngestionServicePort;

/// MinIO/S3 notification.
///
/// [Reference](https://min.io/docs/minio/linux/administration/monitoring/bucket-notifications.html)
#[derive(Debug, Deserialize)]
#[serde(rename_all = "PascalCase")]
struct S3EventNotification {
    /// Array of cloud events.
    records: Vec<S3EventRecord>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct S3EventRecord {
    /// Event verb.
    event_name: String,
    /// Nested target entity.
    s3: S3Entity,
}

#[derive(Debug, Deserialize)]
struct S3Entity {
    bucket: S3Bucket,
    object: S3Object,
}

#[derive(Debug, Deserialize)]
struct S3Bucket {
    name: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)]
struct S3Object {
    /// Storage key path.
    key: String,
    size: Option<i64>,
    e_tag: String,
    content_type: String,
}

/// Consumer adapter executing tasks on brokers payloads.
pub struct KafkaConsumerAdapter {
    consumer: StreamConsumer,
    service: Arc<dyn IngestionServicePort>,
}

impl KafkaConsumerAdapter {
    /// Connects and registers consumer groups against a target topic.
    pub async fn new(
        brokers: &str,
        group_id: &str,
        topic: &str,
        service: Arc<dyn IngestionServicePort>,
    ) -> Result<Self> {
        let config = ClientConfig::new()
            .set("bootstrap.servers", brokers)
            .set("group.id", group_id)
            .set("auto.offset.reset", "earliest")
            .set("enable.auto.commit", "false")
            .clone();

        crate::adapters::spi::kafka::create_topics(&config, topic).await?;

        let consumer: StreamConsumer = config.create()?;
        consumer.subscribe(&[topic])?;

        tracing::info!(?brokers, ?group_id, ?topic, "kafka consumer ready");

        Ok(Self { consumer, service })
    }

    /// Primary orchestration block tracking incoming data.
    pub async fn run(&self) -> Result<()> {
        tracing::info!("listening to kafka events...");
        loop {
            match self.consumer.recv().await {
                Ok(message) => {
                    let payload = match message.payload() {
                        Some(p) => p,
                        None => {
                            tracing::error!("Empty message payload");
                            continue;
                        },
                    };

                    match self.handle_message(payload).await {
                        Ok(()) => {
                            if let Err(err) = self
                                .consumer
                                .commit_message(&message, CommitMode::Sync)
                            {
                                tracing::error!(
                                    ?err,
                                    "failed to commit message offset"
                                );
                            }
                        },
                        Err(err) => {
                            tracing::error!(
                                ?err,
                                offset = message.offset(),
                                "failed to process message at offset"
                            );
                        },
                    }
                },
                Err(err) => {
                    tracing::error!(?err, "kafka error");
                    tokio::time::sleep(Duration::from_secs(1)).await;
                },
            }
        }
    }

    async fn handle_message(&self, payload: &[u8]) -> Result<()> {
        let notification: S3EventNotification =
            serde_json::from_slice(payload)
                .context("Failed to deserialize S3 event notification")?;

        let mut errors = Vec::new();

        for record in notification.records {
            if !record.event_name.starts_with("s3:ObjectCreated") {
                continue;
            }

            let bucket = record.s3.bucket.name;
            let key = urlencoding::decode(&record.s3.object.key)
                .map(|decoded| decoded.into_owned())
                .unwrap_or(record.s3.object.key);

            let path_buf = std::path::Path::new(&key);
            if path_buf.components().any(|c| {
                matches!(
                    c,
                    std::path::Component::ParentDir |
                        std::path::Component::RootDir
                )
            }) {
                errors.push(anyhow!(
                    "Path traversal attempt detected in key: {key}"
                ));
                continue;
            }

            tracing::info!(%bucket, %key, "new file detected from notification");
            if let Err(err) = self.service.process(bucket, key).await {
                errors.push(err);
            }
        }

        if !errors.is_empty() {
            return Err(anyhow!("Batch processing failures: {errors:?}"));
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use async_trait::async_trait;

    use super::*;

    struct MockIngestionService {
        processed: Mutex<Vec<(String, String)>>,
        should_fail: bool,
    }

    #[async_trait]
    impl IngestionServicePort for MockIngestionService {
        async fn process(&self, bucket: String, key: String) -> Result<()> {
            if self.should_fail {
                return Err(anyhow!("Database timeout"));
            }
            self.processed.lock().unwrap().push((bucket, key));
            Ok(())
        }
    }

    fn create_test_adapter(
        service: Arc<dyn IngestionServicePort>,
    ) -> KafkaConsumerAdapter {
        let config = ClientConfig::new()
            .set("bootstrap.servers", "localhost:9092")
            .set("group.id", "test-group")
            .clone();
        let consumer: StreamConsumer = config.create().unwrap();
        KafkaConsumerAdapter { consumer, service }
    }

    #[tokio::test]
    async fn test_handle_message_success() {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: false,
        });
        let adapter = create_test_adapter(service.clone());

        let payload = r#"{
            "Records": [
                {
                    "eventName": "s3:ObjectCreated:Put",
                    "s3": {
                        "bucket": { "name": "production-bucket" },
                        "object": { "key": "uploads%2Fdata.csv", "eTag": "abc", "contentType": "text/csv" }
                    }
                }
            ]
        }"#.as_bytes();

        let result = adapter.handle_message(payload).await;
        assert!(result.is_ok());

        let processed = service.processed.lock().unwrap();
        assert_eq!(processed.len(), 1);
        assert_eq!(processed[0].0, "production-bucket");
        assert_eq!(processed[0].1, "uploads/data.csv");
    }

    #[tokio::test]
    async fn test_handle_message_skips_non_creation_events() {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: false,
        });
        let adapter = create_test_adapter(service.clone());

        let payload = r#"{
            "Records": [
                {
                    "eventName": "s3:ObjectRemoved:Delete",
                    "s3": {
                        "bucket": { "name": "production-bucket" },
                        "object": { "key": "uploads%2Fdata.csv", "eTag": "abc", "contentType": "text/csv" }
                    }
                }
            ]
        }"#.as_bytes();

        let result = adapter.handle_message(payload).await;
        assert!(result.is_ok());

        let processed = service.processed.lock().unwrap();
        assert!(processed.is_empty());
    }

    #[tokio::test]
    async fn test_handle_message_path_traversal_protection() {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: false,
        });
        let adapter = create_test_adapter(service.clone());

        let payload = r#"{
            "Records": [
                {
                    "eventName": "s3:ObjectCreated:Put",
                    "s3": {
                        "bucket": { "name": "production-bucket" },
                        "object": { "key": "..%2F..%2F..%2Fetc%2Fpasswd", "eTag": "abc", "contentType": "text/plain" }
                    }
                }
            ]
        }"#.as_bytes();

        let result = adapter.handle_message(payload).await;
        assert!(result.is_err());

        let processed = service.processed.lock().unwrap();
        assert!(processed.is_empty());
    }

    #[tokio::test]
    async fn test_handle_message_batch_fault_isolation() {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: true,
        });
        let adapter = create_test_adapter(service.clone());

        let payload = r#"{
            "Records": [
                {
                    "eventName": "s3:ObjectCreated:Put",
                    "s3": {
                        "bucket": { "name": "bucket-1" },
                        "object": { "key": "file1.json", "eTag": "1", "contentType": "application/json" }
                    }
                },
                {
                    "eventName": "s3:ObjectCreated:Put",
                    "s3": {
                        "bucket": { "name": "bucket-1" },
                        "object": { "key": "file2.json", "eTag": "2", "contentType": "application/json" }
                    }
                }
            ]
        }"#.as_bytes();

        let result = adapter.handle_message(payload).await;
        assert!(result.is_err());
        let err_msg = format!("{:?}", result.err().unwrap());
        assert!(err_msg.contains("Database timeout"));
    }
}
