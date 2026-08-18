//! Kafka consumer for incoming bucket event handling.

use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use chrono::Utc;
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::propagation::Extractor;
use opentelemetry::{Context as OtelContext, KeyValue, global};
use rdkafka::config::ClientConfig;
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use rdkafka::message::{Headers, Message};
use serde::Deserialize;
use tracing::Instrument as _;
use tracing_opentelemetry::OpenTelemetrySpanExt as _;

use crate::domain::models::FileEvent;
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
    /// Object-store event timestamp retained across Kafka replay.
    #[serde(default)]
    event_time: Option<chrono::DateTime<Utc>>,
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
    #[serde(default)]
    size: Option<i64>,
    #[serde(default)]
    e_tag: String,
    #[serde(default)]
    content_type: String,
}

/// Consumer adapter executing tasks on brokers payloads.
pub struct KafkaConsumerAdapter {
    consumer: StreamConsumer,
    metrics: ConsumerMetrics,
    service: Arc<dyn IngestionServicePort>,
}

struct ConsumerMetrics {
    messages: Counter<u64>,
    duration: Histogram<f64>,
}

impl ConsumerMetrics {
    fn new() -> Self {
        let meter = global::meter("galadril.intake.kafka");
        Self {
            messages: meter
                .u64_counter("messaging.process.count")
                .with_description("number of processed kafka messages")
                .build(),
            duration: meter
                .f64_histogram("messaging.process.duration")
                .with_description("kafka message processing duration")
                .with_unit("s")
                .build(),
        }
    }

    #[inline]
    fn record(&self, started_at: Instant, succeeded: bool) {
        let attributes = [KeyValue::new(
            "messaging.process.status",
            if succeeded { "success" } else { "error" },
        )];
        self.messages.add(1, &attributes);
        self.duration
            .record(started_at.elapsed().as_secs_f64(), &attributes);
    }
}

struct KafkaHeaderExtractor<'a, H: Headers>(&'a H);

impl<H: Headers> Extractor for KafkaHeaderExtractor<'_, H> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.iter().find_map(|header| {
            if !header.key.eq_ignore_ascii_case(key) {
                return None;
            }
            header
                .value
                .and_then(|value| std::str::from_utf8(value).ok())
        })
    }

    fn keys(&self) -> Vec<&str> {
        self.0.iter().map(|header| header.key).collect()
    }
}

fn parent_context<H: Headers>(headers: Option<&H>) -> OtelContext {
    headers.map_or_else(OtelContext::new, |headers| {
        global::get_text_map_propagator(|propagator| {
            propagator.extract(&KafkaHeaderExtractor(headers))
        })
    })
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

        tracing::info!(
            event.name = "kafka.consumer.ready",
            ?brokers,
            ?group_id,
            ?topic,
            "kafka consumer ready"
        );

        Ok(Self {
            consumer,
            metrics: ConsumerMetrics::new(),
            service,
        })
    }

    /// Primary orchestration block tracking incoming data.
    pub async fn run(&self) -> Result<()> {
        tracing::info!(
            event.name = "kafka.consumer.listening",
            "listening to kafka events"
        );
        loop {
            match self.consumer.recv().await {
                Ok(message) => {
                    let payload = match message.payload() {
                        Some(p) => p,
                        None => {
                            tracing::error!(
                                event.name = "kafka.message.empty",
                                "empty message payload"
                            );
                            continue;
                        },
                    };

                    let span = tracing::info_span!(
                        "kafka.message.process",
                        otel.kind = "consumer",
                        messaging.system = "kafka",
                        messaging.destination.name = %message.topic(),
                        messaging.kafka.offset = message.offset(),
                    );
                    if let Err(error) =
                        span.set_parent(parent_context(message.headers()))
                    {
                        tracing::warn!(
                            event.name = "trace.parent.rejected",
                            error = %error,
                            "incoming kafka trace parent rejected"
                        );
                    }
                    let started_at = Instant::now();
                    let result =
                        self.handle_message(payload).instrument(span).await;
                    self.metrics.record(started_at, result.is_ok());
                    match result {
                        Ok(()) => {
                            if let Err(err) = self
                                .consumer
                                .commit_message(&message, CommitMode::Sync)
                            {
                                tracing::error!(
                                    event.name = "kafka.offset.commit.failed",
                                    ?err,
                                    "failed to commit message offset"
                                );
                            }
                        },
                        Err(err) => {
                            tracing::error!(
                                event.name = "kafka.message.failed",
                                ?err,
                                offset = message.offset(),
                                "failed to process message at offset"
                            );
                        },
                    }
                },
                Err(err) => {
                    tracing::error!(
                        event.name = "kafka.consumer.failed",
                        ?err,
                        "kafka consumer failed"
                    );
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
                .unwrap_or_else(|_| record.s3.object.key.clone());

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

            tracing::info!(
                event.name = "storage.object.detected",
                %bucket,
                %key,
                "new object detected from notification"
            );
            let event = FileEvent {
                bucket,
                key,
                size: record.s3.object.size.unwrap_or(0),
                e_tag: record.s3.object.e_tag,
                content_type: record.s3.object.content_type,
                event_name: record.event_name,
                received_at: record.event_time.unwrap_or_else(Utc::now),
            };
            if let Err(err) = self.service.process(event).await {
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
    use opentelemetry::trace::TraceContextExt as _;
    use opentelemetry_sdk::propagation::TraceContextPropagator;
    use rdkafka::message::{Header, OwnedHeaders};

    use super::*;

    #[test]
    fn kafka_headers_preserve_w3c_remote_parent() {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let value = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
        let headers = OwnedHeaders::new().insert(Header {
            key: "traceparent",
            value: Some(value),
        });

        let context = parent_context(Some(&headers));
        let span = context.span();

        assert!(span.span_context().is_remote());
        assert_eq!(
            span.span_context().trace_id().to_string(),
            "4bf92f3577b34da6a3ce929d0e0e4736"
        );
    }

    struct MockIngestionService {
        processed: Mutex<Vec<FileEvent>>,
        should_fail: bool,
    }

    #[async_trait]
    impl IngestionServicePort for MockIngestionService {
        async fn process(&self, event: FileEvent) -> Result<()> {
            if self.should_fail {
                return Err(anyhow!("Database timeout"));
            }
            self.processed
                .lock()
                .map_err(|_| anyhow!("test ingestion lock poisoned"))?
                .push(event);
            Ok(())
        }
    }

    fn create_test_adapter(
        service: Arc<dyn IngestionServicePort>,
    ) -> Result<KafkaConsumerAdapter> {
        let config = ClientConfig::new()
            .set("bootstrap.servers", "localhost:9092")
            .set("group.id", "test-group")
            .clone();
        let consumer: StreamConsumer = config.create()?;
        Ok(KafkaConsumerAdapter {
            consumer,
            metrics: ConsumerMetrics::new(),
            service,
        })
    }

    #[tokio::test]
    async fn test_handle_message_success() -> Result<()> {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: false,
        });
        let adapter = create_test_adapter(service.clone())?;

        let payload = r#"{
            "Records": [
                {
                    "eventName": "s3:ObjectCreated:Put",
                    "eventTime": "2026-08-18T12:34:56Z",
                    "s3": {
                        "bucket": { "name": "production-bucket" },
                        "object": { "key": "uploads%2Fdata.csv", "eTag": "abc", "contentType": "text/csv" }
                    }
                }
            ]
        }"#.as_bytes();

        let result = adapter.handle_message(payload).await;
        assert!(result.is_ok());

        let processed = service
            .processed
            .lock()
            .map_err(|_| anyhow!("test ingestion lock poisoned"))?;
        assert_eq!(processed.len(), 1);
        assert_eq!(processed[0].bucket, "production-bucket");
        assert_eq!(processed[0].key, "uploads/data.csv");
        assert_eq!(processed[0].e_tag, "abc");
        assert_eq!(processed[0].content_type, "text/csv");
        assert_eq!(
            processed[0].received_at.to_rfc3339(),
            "2026-08-18T12:34:56+00:00"
        );
        Ok(())
    }

    #[tokio::test]
    async fn test_handle_message_skips_non_creation_events() -> Result<()> {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: false,
        });
        let adapter = create_test_adapter(service.clone())?;

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

        let processed = service.processed.lock();
        assert!(processed.is_ok());
        if let Ok(processed) = processed {
            assert!(processed.is_empty());
        }
        Ok(())
    }

    #[tokio::test]
    async fn test_handle_message_path_traversal_protection() -> Result<()> {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: false,
        });
        let adapter = create_test_adapter(service.clone())?;

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

        let processed = service.processed.lock();
        assert!(processed.is_ok());
        if let Ok(processed) = processed {
            assert!(processed.is_empty());
        }
        Ok(())
    }

    #[tokio::test]
    async fn test_handle_message_batch_fault_isolation() -> Result<()> {
        let service = Arc::new(MockIngestionService {
            processed: Mutex::new(vec![]),
            should_fail: true,
        });
        let adapter = create_test_adapter(service.clone())?;

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
        let Some(error) = result.err() else {
            return Err(anyhow!("expected batch processing to fail"));
        };
        assert!(format!("{error:?}").contains("Database timeout"));
        Ok(())
    }
}
