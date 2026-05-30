//! Kafka producer.

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{Context, Result, anyhow};
use async_trait::async_trait;
use rdkafka::config::ClientConfig;
use rdkafka::producer::{FutureProducer, FutureRecord};
use schema_registry_converter::async_impl::avro::AvroEncoder;
use schema_registry_converter::async_impl::schema_registry::{
    SrSettings, post_schema,
};
use schema_registry_converter::schema_registry_common::{
    SchemaType, SubjectNameStrategy, SuppliedReference, SuppliedSchema,
};

use crate::domain::models::SourceConfig;
use crate::domain::ports::EventProducer;

const AUTHZ_TUPLE_SCHEMA_PATH: &str = "schemas/avro/authz_tuple.avsc";
const AUTHZ_SCHEMA_PATH: &str = "schemas/avro/authz.avsc";

const AUTHZ_TUPLE_FULLNAME: &str = "com.galadril.auth.AuthzTuple";
const AUTHZ_FULLNAME: &str = "com.galadril.auth.Authz";

fn subject_for_fullname(fullname: &str) -> String {
    format!("{fullname}-value")
}

/// Heuristic: detect whether a schema depends on Authz types.
fn schema_needs_authz_references(schema_raw: &str) -> bool {
    schema_raw.contains(AUTHZ_FULLNAME) ||
        schema_raw.contains(AUTHZ_TUPLE_FULLNAME)
}

pub struct KafkaProducerAdapter {
    producer: FutureProducer,
    encoder: AvroEncoder<'static>,
    schema_names: HashMap<String, String>,
}

impl KafkaProducerAdapter {
    /// Create a new [`KafkaProducerAdapter`].
    pub async fn new(
        brokers: &str,
        registry_url: &str,
        sources: &[SourceConfig],
    ) -> Result<Self> {
        let config = ClientConfig::new()
            .set("bootstrap.servers", brokers)
            .set("message.timeout.ms", "5000")
            .set("acks", "all")
            .clone();

        for source in sources {
            crate::adapters::spi::kafka::create_topics(&config, &source.topic)
                .await?;
        }

        let producer: FutureProducer =
            config.create().context("Failed to create Kafka producer")?;

        let sr_settings =
            SrSettings::new_builder(registry_url.to_string()).build()?;
        let schema_names =
            Self::register_schemas(&sr_settings, sources).await?;
        let encoder = AvroEncoder::new(sr_settings);

        tracing::info!(?brokers, "kafka producer ready");

        Ok(Self {
            producer,
            encoder,
            schema_names,
        })
    }

    async fn register_schemas(
        sr_settings: &SrSettings,
        sources: &[SourceConfig],
    ) -> Result<HashMap<String, String>> {
        let mut schema_mapping = HashMap::new();

        Self::try_register_global_schema(sr_settings, AUTHZ_TUPLE_SCHEMA_PATH)
            .await?;
        Self::try_register_global_schema(sr_settings, AUTHZ_SCHEMA_PATH)
            .await?;

        for source in sources {
            if let Some(path) = &source.schema_path {
                if schema_mapping.contains_key(path) {
                    continue;
                }

                let schema_raw = std::fs::read_to_string(path)
                    .context(format!("Failed to read schema at {path}"))?;

                let parsed_schema = apache_avro::Schema::parse_str(
                    &schema_raw,
                )
                .context(format!("Failed to parse schema for {path}"))?;

                let record_name = match &parsed_schema {
                    apache_avro::Schema::Record(record) => {
                        record.name.fullname(None)
                    },
                    _ => {
                        return Err(anyhow!(
                            "Schema {path} is not a record type"
                        ));
                    },
                };

                let subject = format!("{record_name}-value");

                let references = if schema_needs_authz_references(&schema_raw)
                {
                    let authz_tuple_schema =
                        std::fs::read_to_string(AUTHZ_TUPLE_SCHEMA_PATH)?;
                    let authz_schema =
                        std::fs::read_to_string(AUTHZ_SCHEMA_PATH)?;

                    vec![
                        SuppliedReference {
                            name: AUTHZ_TUPLE_FULLNAME.to_string(),
                            subject: subject_for_fullname(
                                AUTHZ_TUPLE_FULLNAME,
                            ),
                            schema: authz_tuple_schema,
                            references: vec![],
                            properties: None,
                            tags: None,
                        },
                        SuppliedReference {
                            name: AUTHZ_FULLNAME.to_string(),
                            subject: subject_for_fullname(AUTHZ_FULLNAME),
                            schema: authz_schema,
                            references: vec![],
                            properties: None,
                            tags: None,
                        },
                    ]
                } else {
                    vec![]
                };
                let supplied_schema = SuppliedSchema {
                    name: Some(record_name.clone()),
                    schema_type: SchemaType::Avro,
                    schema: schema_raw,
                    references,
                    properties: None,
                    tags: None,
                };

                post_schema(sr_settings, subject, supplied_schema).await?;
                tracing::info!(
                    ?record_name,
                    "schema registered for path {path}"
                );

                schema_mapping.insert(path.to_string(), record_name);
            }
        }

        Ok(schema_mapping)
    }

    async fn try_register_global_schema(
        sr_settings: &SrSettings,
        path: &str,
    ) -> Result<()> {
        let schema_raw = match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(_) => {
                tracing::warn!(
                    path,
                    "global schema not found on disk; skipping"
                );
                return Ok(());
            },
        };

        let parsed_schema = apache_avro::Schema::parse_str(&schema_raw)
            .context(format!("Failed to parse global schema for {path}"))?;

        let record_name = match &parsed_schema {
            apache_avro::Schema::Record(record) => record.name.fullname(None),
            _ => {
                return Err(anyhow!(
                    "Global schema {path} is not a record type"
                ));
            },
        };

        let subject = format!("{record_name}-value");

        let supplied_schema = SuppliedSchema {
            name: Some(record_name.clone()),
            schema_type: SchemaType::Avro,
            schema: schema_raw,
            references: vec![],
            properties: None,
            tags: None,
        };

        post_schema(sr_settings, subject, supplied_schema).await?;
        tracing::info!(
            ?record_name,
            "global schema registered for path {path}"
        );
        Ok(())
    }
}

#[async_trait]
impl EventProducer for KafkaProducerAdapter {
    async fn publish(
        &self,
        topic: &str,
        schema_path: Option<&str>,
        key: &str,
        payload: &serde_json::Value,
    ) -> Result<()> {
        let encoded = if let Some(path) = schema_path {
            let record_name =
                self.schema_names.get(path).ok_or_else(|| {
                    anyhow!("No registered Avro schema found for {path}")
                })?;
            let strategy =
                SubjectNameStrategy::RecordNameStrategy(record_name.clone());
            self.encoder.encode_struct(payload, &strategy).await?
        } else {
            serde_json::to_vec(payload)?
        };

        let record = FutureRecord::to(topic).key(key).payload(&encoded);

        self.producer
            .send(record, Duration::from_secs(5))
            .await
            .map_err(|(err, _)| anyhow!("Kafka send error: {err:?}"))?;

        tracing::debug!(topic, "event sent");

        Ok(())
    }
}
