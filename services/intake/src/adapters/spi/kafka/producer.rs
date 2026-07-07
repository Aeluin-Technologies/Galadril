//! Async Kafka producer mapping local Avro specs to registries.

use std::collections::HashMap;
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

use crate::domain::models::SourceConfig;
use crate::domain::ports::EventProducer;

const AUTHZ_SCHEMA_PATH: &str = "schemas/avro/authz.avsc";
const AUTHZ_FULLNAME: &str = "com.galadril.auth.Authz";

#[inline]
fn subject_for_fullname(fullname: &str) -> String {
    format!("{fullname}-value")
}

#[inline]
fn schema_needs_authz_references(schema_raw: &str) -> bool {
    schema_raw.contains(AUTHZ_FULLNAME) ||
        schema_raw.contains("com.galadril.auth.AuthzTuple")
}

/// Client broker wrapping encoding strategies and routing metrics.
pub struct KafkaProducerAdapter {
    producer: FutureProducer,
    encoder: AvroEncoder<'static>,
    schema_names: HashMap<String, String>,
}

impl KafkaProducerAdapter {
    /// Prepares cluster hooks and pushes validation parameters.
    pub async fn new(
        brokers: &str,
        registry_url: &str,
        sources: &[SourceConfig],
    ) -> Result<Self> {
        let config = ClientConfig::new()
            .set("bootstrap.servers", brokers)
            .set("message.timeout.ms", "5000")
            .set("acks", "all")
            .set("socket.timeout.ms", "4000")
            .set("metadata.request.timeout.ms", "4000")
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
        let mut schema_mapping = HashMap::with_capacity(sources.len());

        let authz_raw = std::fs::read_to_string(AUTHZ_SCHEMA_PATH)
            .context(format!("Failed to read {AUTHZ_SCHEMA_PATH}"))?;

        apache_avro::Schema::parse_str(&authz_raw)
            .context("Failed to parse global unified authz schema")?;

        let supplied_authz = SuppliedSchema {
            name: Some(AUTHZ_FULLNAME.to_string()),
            schema_type: SchemaType::Avro,
            schema: authz_raw.clone(),
            references: vec![],
            properties: None,
            tags: None,
        };
        post_schema(
            sr_settings,
            subject_for_fullname(AUTHZ_FULLNAME),
            supplied_authz,
        )
        .await?;

        for source in sources {
            if let Some(path) = &source.schema_path {
                if schema_mapping.contains_key(path) {
                    continue;
                }

                let schema_raw = std::fs::read_to_string(path)
                    .context(format!("Failed to read schema at {path}"))?;

                let parsed_schema =
                    if schema_needs_authz_references(&schema_raw) {
                        let list = apache_avro::Schema::parse_list([
                            &authz_raw,
                            &schema_raw,
                        ])
                        .context(format!(
                            "Failed to parse nested dependencies for {path}"
                        ))?;
                        list.into_iter().last().unwrap()
                    } else {
                        apache_avro::Schema::parse_str(&schema_raw).context(
                            format!(
                                "Failed to parse standalone schema for {path}"
                            ),
                        )?
                    };

                let record_name = match &parsed_schema {
                    apache_avro::Schema::Record(record) => {
                        record.name.fullname(None)
                    },
                    _ => bail!(
                        "Schema context located at {path} must contain record roots"
                    ),
                };

                let final_schema_json =
                    if schema_needs_authz_references(&schema_raw) {
                        format!(
                            "[{}, {}]",
                            authz_raw.trim(),
                            schema_raw.trim()
                        )
                    } else {
                        schema_raw
                    };

                let supplied_schema = SuppliedSchema {
                    name: Some(record_name.clone()),
                    schema_type: SchemaType::Avro,
                    schema: final_schema_json,
                    references: vec![],
                    properties: None,
                    tags: None,
                };

                post_schema(sr_settings, record_name.clone(), supplied_schema)
                    .await?;
                tracing::info!(
                    ?record_name,
                    "schema registered for path {path}"
                );

                schema_mapping.insert(path.to_string(), record_name);
            }
        }

        Ok(schema_mapping)
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
            .map_err(|(err, _)| anyhow!("Kafka transfer failure: {err:?}"))?;

        tracing::debug!(%topic, "event sent");
        Ok(())
    }
}
