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
    SrSettings, perform_sr_call, post_schema,
};
use schema_registry_converter::schema_registry_common::{
    SchemaType, SrCall, SubjectNameStrategy, SuppliedSchema,
};
use serde::Serialize;

use crate::domain::ports::EventProducer;

const AUTHZ_SCHEMA_PREFIX: &str = "authz";

/// Maps filenames, full names, or paths to schema subject names.
pub struct KafkaProducerAdapter {
    producer: FutureProducer,
    encoder: AvroEncoder<'static>,
    resolution_cache: HashMap<String, String>,
}

struct RegisteredInfo {
    subject: String,
    content: String,
    version: u32,
}

#[derive(Serialize)]
struct RegistryReference<'a> {
    name: &'a str,
    subject: &'a str,
    version: u32,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RegistrySchemaBody<'a> {
    schema: &'a str,
    schema_type: &'static str,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    references: Vec<RegistryReference<'a>>,
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
        prioritize_authz_schemas(&mut pending);

        let mut cache = HashMap::with_capacity(pending.len() * 3);
        let mut registered_schemas: HashMap<String, RegisteredInfo> =
            HashMap::with_capacity(pending.len());
        let mut progress = true;

        while !pending.is_empty() && progress {
            progress = false;
            let mut unresolvable = Vec::with_capacity(pending.len());

            for (path, content) in pending {
                let mut compilation_context: Vec<&str> = registered_schemas
                    .iter()
                    .filter(|(name, _)| content.contains(name.as_str()))
                    .map(|(_, info)| info.content.as_str())
                    .collect();
                compilation_context.push(&content);

                match apache_avro::Schema::parse_list(&compilation_context) {
                    Ok(parsed_list) => {
                        if let Some(apache_avro::Schema::Record(
                            record_schema,
                        )) = parsed_list.into_iter().last()
                        {
                            let fullname = record_schema.name.fullname(None);
                            let subject = format!("{fullname}-value");

                            let references: Vec<RegistryReference<'_>> =
                                registered_schemas
                                    .iter()
                                    .filter(|(name, _)| {
                                        content.contains(name.as_str())
                                    })
                                    .map(|(name, info)| RegistryReference {
                                        name,
                                        subject: &info.subject,
                                        version: info.version,
                                    })
                                    .collect();

                            let version = register_schema(
                                sr_settings,
                                &subject,
                                &fullname,
                                &content,
                                references,
                            )
                            .await
                            .with_context(|| {
                                format!(
                                    "failed pushing schema {:?} to registry",
                                    path
                                )
                            })?;

                            registered_schemas.insert(
                                fullname.clone(),
                                RegisteredInfo {
                                    subject,
                                    content: content.clone(),
                                    version,
                                },
                            );

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

#[inline]
fn is_authz_schema(path: &std::path::Path) -> bool {
    path.file_stem()
        .and_then(|stem| stem.to_str())
        .is_some_and(|stem| {
            stem.as_bytes()
                .get(..AUTHZ_SCHEMA_PREFIX.len())
                .is_some_and(|prefix| {
                    prefix.eq_ignore_ascii_case(AUTHZ_SCHEMA_PREFIX.as_bytes())
                })
        })
}

fn prioritize_authz_schemas(pending: &mut [(PathBuf, String)]) {
    pending.sort_unstable_by_key(|(path, _)| !is_authz_schema(path));
}

fn serialize_registry_body(
    content: &str,
    references: Vec<RegistryReference<'_>>,
) -> Result<String> {
    serde_json::to_string(&RegistrySchemaBody {
        schema: content,
        schema_type: "AVRO",
        references,
    })
    .context("failed serializing schema registry request")
}

async fn register_schema(
    sr_settings: &SrSettings,
    subject: &str,
    fullname: &str,
    content: &str,
    references: Vec<RegistryReference<'_>>,
) -> Result<u32> {
    let has_references = !references.is_empty();
    let body = serialize_registry_body(content, references)?;

    if has_references {
        let registered =
            perform_sr_call(sr_settings, SrCall::PostNew(subject, &body))
                .await?;
        if registered.id.is_none() {
            bail!("schema registry omitted the id for {subject}");
        }
    } else {
        let supplied = SuppliedSchema {
            name: Some(fullname.to_owned()),
            schema_type: SchemaType::Avro,
            schema: content.to_owned(),
            references: vec![],
            properties: None,
            tags: None,
        };
        post_schema(sr_settings, subject.to_owned(), supplied).await?;
    }

    perform_sr_call(sr_settings, SrCall::PostForVersion(subject, &body))
        .await?
        .version
        .ok_or_else(|| {
            anyhow!("schema registry omitted the version for {subject}")
        })
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

    type PublishedCall = (String, Option<String>, String, serde_json::Value);
    type PublishedCalls = Vec<PublishedCall>;

    struct EventProducerMock {
        pub calls: Mutex<PublishedCalls>,
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
            let mut lock = self
                .calls
                .lock()
                .map_err(|_| anyhow!("event producer mock lock poisoned"))?;
            lock.push((
                topic.to_string(),
                schema_ref.map(ToString::to_string),
                key.to_string(),
                payload.clone(),
            ));
            Ok(())
        }
    }

    #[test]
    fn test_prioritize_authz_schemas_before_other_schemas() {
        let mut schemas = vec![
            (PathBuf::from("audio.avsc"), String::new()),
            (PathBuf::from("authz_tuple.avsc"), String::new()),
            (PathBuf::from("document.avsc"), String::new()),
            (PathBuf::from("authz.avsc"), String::new()),
        ];

        prioritize_authz_schemas(&mut schemas);

        assert!(schemas[..2].iter().all(|(path, _)| is_authz_schema(path)));
        assert!(schemas[2..].iter().all(|(path, _)| !is_authz_schema(path)));
    }

    #[test]
    fn test_registry_reference_body_omits_unsupported_metadata() {
        let references = vec![RegistryReference {
            name: "com.galadril.auth.Authz",
            subject: "com.galadril.auth.Authz-value",
            version: 1,
        }];

        let result = serialize_registry_body("{}", references);

        assert!(result.is_ok());
        if let Ok(body) = result {
            assert!(body.contains(
                r#""references":[{"name":"com.galadril.auth.Authz","subject":"com.galadril.auth.Authz-value","version":1}]"#
            ));
            assert!(!body.contains("properties"));
            assert!(!body.contains("tags"));
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

        let calls = mock.calls.lock();
        assert!(calls.is_ok());
        if let Ok(calls) = calls {
            assert_eq!(calls.len(), 1);
            assert_eq!(calls[0].0, "test-topic");
            assert_eq!(
                calls[0].1.as_deref(),
                Some("com.galadril.user.Profile")
            );
            assert_eq!(calls[0].2, "key-1");
            assert_eq!(calls[0].3, test_payload);
        }
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

        let calls = mock.calls.lock();
        assert!(calls.is_ok());
        if let Ok(calls) = calls {
            assert_eq!(calls.len(), 1);
            assert_eq!(calls[0].1.as_deref(), Some("user.avsc"));
        }
    }

    #[tokio::test]
    async fn test_compile_and_register_schemas_fails_on_missing_reference() {
        let sr_settings =
            SrSettings::new_builder("http://localhost:8081".to_string())
                .build();
        assert!(sr_settings.is_ok());
        let Ok(sr_settings) = sr_settings else {
            return;
        };

        let raw_schemas = vec![(
            PathBuf::from("audio.avsc"),
            r#"{
                "type": "record",
                "name": "Audio",
                "namespace": "com.galadril.raw",
                "fields": [{
                    "name": "authz",
                    "type": "com.galadril.auth.UnknownType"
                }]
            }"#
            .to_string(),
        )];

        let result = KafkaProducerAdapter::compile_and_register_schemas(
            &sr_settings,
            raw_schemas,
        )
        .await;

        assert!(result.is_err());
        if let Err(err) = result {
            let err = err.to_string();
            assert!(err.contains("circular dependency or missing references"));
        }
    }

    #[tokio::test]
    async fn test_compile_and_register_schemas_fails_on_non_record_root() {
        let sr_settings =
            SrSettings::new_builder("http://localhost:8081".to_string())
                .build();
        assert!(sr_settings.is_ok());
        let Ok(sr_settings) = sr_settings else {
            return;
        };

        let raw_schemas = vec![(
            PathBuf::from("enum.avsc"),
            r#"{
                "type": "enum",
                "name": "Status",
                "symbols": ["ACTIVE", "INACTIVE"]
            }"#
            .to_string(),
        )];

        let result = KafkaProducerAdapter::compile_and_register_schemas(
            &sr_settings,
            raw_schemas,
        )
        .await;

        assert!(result.is_err());
        if let Err(err) = result {
            let err = err.to_string();
            assert!(err.contains("avro root inside"));
            assert!(err.contains("must be a record"));
        }
    }
}
