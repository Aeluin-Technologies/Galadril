//! Kafka consumer.

use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use rdkafka::Message;
use rdkafka::config::ClientConfig;
use rdkafka::consumer::{Consumer, StreamConsumer};
use serde::Deserialize;

use crate::domain::ports::IngestionServicePort;

/// MinIO/S3 notification.
///
/// [Reference](https://min.io/docs/minio/linux/administration/monitoring/bucket-notifications.html)
#[derive(Debug, Deserialize)]
#[serde(rename_all = "PascalCase")]
struct S3EventNotification {
    records: Vec<S3EventRecord>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct S3EventRecord {
    event_name: String,
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
struct S3Object {
    key: String,
    size: Option<i64>,
    e_tag: String,
    content_type: String,
}

pub struct KafkaConsumerAdapter {
    consumer: StreamConsumer,
    service: Arc<dyn IngestionServicePort>,
}

impl KafkaConsumerAdapter {
    /// Create a new [`KafkaConsumerAdapter`].
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
            .set("enable.auto.commit", "true")
            .clone();

        crate::adapters::spi::kafka::create_topics(&config, topic).await?;

        let consumer: StreamConsumer = config.create()?;

        consumer.subscribe(&[topic])?;

        tracing::info!(?brokers, ?group_id, ?topic, "kafka consumer ready");

        Ok(Self { consumer, service })
    }

    /// Listening loop.
    pub async fn run(&self) -> Result<()> {
        tracing::info!("listening to kafka events...");
        loop {
            match self.consumer.recv().await {
                Ok(message) => {
                    let payload = message
                        .payload()
                        .ok_or_else(|| anyhow!("Empty message payload"))?;

                    if let Err(err) = self.handle_message(payload).await {
                        // TODO: send to DLQ.
                        tracing::error!(
                            ?err,
                            offset = message.offset(),
                            "failed to process message at offset"
                        );
                    }
                },
                Err(err) => {
                    tracing::error!(?err, "kafka error");
                },
            }
        }
    }

    async fn handle_message(&self, payload: &[u8]) -> Result<()> {
        let notification: S3EventNotification =
            serde_json::from_slice(payload)
                .context("Failed to deserialize S3 event notification")?;

        for record in notification.records {
            if !record.event_name.starts_with("s3:ObjectCreated") {
                continue;
            }

            let bucket = record.s3.bucket.name;
            let key = record.s3.object.key;
            let key = urlencoding::decode(&key)
                .map(|decoded| decoded.into_owned())
                .unwrap_or(key);

            let url = format!("s3://{bucket}/{key}");
            tracing::info!(url, "new file detected");

            self.service.process(bucket, key).await?;
        }

        Ok(())
    }
}
