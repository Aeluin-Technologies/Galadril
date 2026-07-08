//! Kafka producer with dynamic schema resolution and Avro encoding.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use async_trait::async_trait;
use rdkafka::config::ClientConfig;
use rdkafka::producer::{FutureProducer, FutureRecord};
use schema_registry_converter::async_impl::avro::AvroEncoder;
use schema_registry_converter::async_impl::schema_registry::{
    SrSettings, post_schema,
};
use schema_registry_converter::schema_registry_common::{
    SchemaType, SubjectNameStrategy, SuppliedSchema,
};

use crate::domain::ports::EventProducer;

/// Maps filenames, full names, or paths to schema subject names.
pub struct KafkaProducerAdapter {
    producer: FutureProducer,
    encoder: AvroEncoder<'static>,
    resolution_cache: HashMap<String, String>,
}

impl KafkaProducerAdapter {
    /// Creates a producer and registers local schemas with the registry.
    pub async fn new(
        brokers: &str,
        registry_url: &str,
        raw_schemas: Vec<(PathBuf, String)>,
    ) -> Result<Self> {
        let config = ClientConfig::new()
            .set("bootstrap.servers", brokers)
            .set("message.timeout.ms", "5000")
            .set("acks", "all")
            .set("socket.timeout.ms", "4000")
            .set("metadata.request.timeout.ms", "4000")
            .clone();

        let producer: FutureProducer =
            config.create().context("Failed to create Kafka producer")?;

        let sr_settings =
            SrSettings::new_builder(registry_url.to_string()).build()?;
        let resolution_cache =
            Self::compile_and_register_schemas(&sr_settings, raw_schemas)
                .await?;
        let encoder = AvroEncoder::new(sr_settings);

        tracing::info!(
            ?brokers,
            schema_cache_size = resolution_cache.len(),
            "kafka producer ready"
        );

        Ok(Self {
            producer,
            encoder,
            resolution_cache,
        })
    }

    /// Resolves and registers interdependent schemas iteratively.
    async fn compile_and_register_schemas(
        sr_settings: &SrSettings,
        mut pending: Vec<(PathBuf, String)>,
    ) -> Result<HashMap<String, String>> {
        let mut cache = HashMap::with_capacity(pending.len() * 3);
        let mut parsed_contents = Vec::with_capacity(pending.len());
        let mut progress = true;

        while !pending.is_empty() && progress {
            progress = false;
            let mut unresolvable = Vec::with_capacity(pending.len());

            for (path, content) in pending {
                let mut compilation_context: Vec<&str> =
                    parsed_contents.iter().map(AsRef::as_ref).collect();
                compilation_context.push(&content);

                match apache_avro::Schema::parse_list(&compilation_context) {
                    Ok(parsed_list) => {
                        if let Some(apache_avro::Schema::Record(
                            record_schema,
                        )) = parsed_list.into_iter().last()
                        {
                            let fullname = record_schema.name.fullname(None);
                            let subject = format!("{fullname}-value");

                            let supplied = SuppliedSchema {
                                name: Some(fullname.clone()),
                                schema_type: SchemaType::Avro,
                                schema: content.clone(),
                                references: vec![],
                                properties: None,
                                tags: None,
                            };

                            post_schema(sr_settings, subject, supplied)
                                .await
                                .with_context(|| format!("failed pushing schema {:?} to registry", path))?;

                            cache.insert(fullname.clone(), fullname.clone());
                            cache.insert(
                                path.to_string_lossy().into_owned(),
                                fullname.clone(),
                            );
                            if let Some(filename) =
                                path.file_name().and_then(|f| f.to_str())
                            {
                                cache.insert(
                                    filename.to_owned(),
                                    fullname.clone(),
                                );
                            }

                            parsed_contents.push(content);
                            progress = true;
                            tracing::info!(
                                ?path,
                                ?fullname,
                                "schema bound and registered"
                            );
                        } else {
                            bail!(
                                "avro root inside {:?} must be a record",
                                path
                            );
                        }
                    },
                    Err(_) => {
                        tracing::debug!(
                            ?path,
                            "schema resolution delayed due to missing references"
                        );
                        unresolvable.push((path, content));
                    },
                }
            }
            pending = unresolvable;
        }

        if !pending.is_empty() {
            let failed_paths: Vec<_> =
                pending.into_iter().map(|(p, _)| p).collect();
            tracing::error!(
                ?failed_paths,
                "schema registry bootstrap aborted"
            );
            bail!(
                "circular dependency or missing references inside: {:?}",
                failed_paths
            );
        }

        Ok(cache)
    }
}

#[async_trait]
impl EventProducer for KafkaProducerAdapter {
    async fn publish(
        &self,
        topic: &str,
        schema_ref: Option<&str>,
        key: &str,
        payload: &serde_json::Value,
    ) -> Result<()> {
        let encoded = if let Some(reference) = schema_ref {
            let target_fullname = self
                .resolution_cache
                .get(reference)
                .map(AsRef::as_ref)
                .unwrap_or(reference);

            let strategy = SubjectNameStrategy::RecordNameStrategy(
                target_fullname.to_string(),
            );
            self.encoder.encode_struct(payload, &strategy).await?
        } else {
            serde_json::to_vec(payload)?
        };

        let record = FutureRecord::to(topic).key(key).payload(&encoded);

        self.producer
            .send(record, Duration::from_secs(5))
            .await
            .map_err(|(err, _)| anyhow!("kafka transfer failure: {err:?}"))?;

        tracing::debug!(%topic, "event published");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    struct EventProducerMock {
        pub calls:
            Mutex<Vec<(String, Option<String>, String, serde_json::Value)>>,
    }

    #[async_trait]
    impl EventProducer for EventProducerMock {
        async fn publish(
            &self,
            topic: &str,
            schema_ref: Option<&str>,
            key: &str,
            payload: &serde_json::Value,
        ) -> Result<()> {
            let mut lock = self.calls.lock().unwrap();
            lock.push((
                topic.to_string(),
                schema_ref.map(|s| s.to_string()),
                key.to_string(),
                payload.clone(),
            ));
            Ok(())
        }
    }

    #[tokio::test]
    async fn test_mock_event_producer_publish() {
        let mock = EventProducerMock {
            calls: Mutex::new(vec![]),
        };
        let test_payload = serde_json::json!({"id": "123"});

        let result = mock
            .publish(
                "test-topic",
                Some("com.galadril.user.Profile"),
                "key-1",
                &test_payload,
            )
            .await;
        assert!(result.is_ok());

        let calls = mock.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "test-topic");
        assert_eq!(calls[0].1, Some("com.galadril.user.Profile".to_string()));
        assert_eq!(calls[0].2, "key-1");
        assert_eq!(calls[0].3, test_payload);
    }

    #[tokio::test]
    async fn test_mock_event_producer_publish_with_file_path_ref() {
        let mock = EventProducerMock {
            calls: Mutex::new(vec![]),
        };
        let test_payload = serde_json::json!({"status": "active"});

        let result = mock
            .publish(
                "analytics-topic",
                Some("user.avsc"),
                "key-2",
                &test_payload,
            )
            .await;
        assert!(result.is_ok());

        let calls = mock.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1, Some("user.avsc".to_string()));
    }
}
