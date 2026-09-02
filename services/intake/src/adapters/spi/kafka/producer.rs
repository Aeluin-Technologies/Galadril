//! Kafka producer with dynamic schema resolution and Avro encoding.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use async_trait::async_trait;
use rdkafka::config::ClientConfig;
use rdkafka::message::{Header, OwnedHeaders};
use rdkafka::producer::{FutureProducer, FutureRecord};
use schema_registry_converter::async_impl::avro::AvroEncoder;
use schema_registry_converter::async_impl::schema_registry::{
    SrSettings, perform_sr_call, post_schema,
};
use schema_registry_converter::schema_registry_common::{
    SchemaType, SrCall, SubjectNameStrategy, SuppliedSchema,
};
use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::domain::ports::EventProducer;
use crate::telemetry::current_w3c_carrier;

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
    dependencies: Vec<String>,
}

#[derive(Debug)]
struct SchemaDescriptor {
    path: PathBuf,
    content: String,
    fullname: String,
    dependencies: Vec<String>,
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
            event.name = "kafka.producer.ready",
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

    /// Registers schemas in explicit dependency order.
    async fn compile_and_register_schemas(
        sr_settings: &SrSettings,
        raw_schemas: Vec<(PathBuf, String)>,
    ) -> Result<HashMap<String, String>> {
        let registration_plan = schema_registration_plan(raw_schemas)?;
        let mut cache = HashMap::with_capacity(registration_plan.len() * 3);
        let mut registered_schemas: HashMap<String, RegisteredInfo> =
            HashMap::with_capacity(registration_plan.len());

        for descriptor in registration_plan {
            let mut compilation_context = Vec::new();
            let mut included = HashSet::new();
            for dependency in &descriptor.dependencies {
                append_compilation_dependency(
                    dependency,
                    &registered_schemas,
                    &mut included,
                    &mut compilation_context,
                )?;
            }
            compilation_context.push(&descriptor.content);
            let parsed = apache_avro::Schema::parse_list(&compilation_context)
                .with_context(|| {
                    format!(
                        "failed compiling Avro schema {:?}",
                        descriptor.path
                    )
                })?;
            if !matches!(parsed.last(), Some(apache_avro::Schema::Record(_))) {
                bail!(
                    "avro root inside {:?} must be a record",
                    descriptor.path
                );
            }

            let subject = format!("{}-value", descriptor.fullname);
            let references =
                descriptor
                    .dependencies
                    .iter()
                    .map(|name| {
                        let info = registered_schemas.get(name).ok_or_else(|| {
                        anyhow!("schema dependency {name} was not registered")
                    })?;
                        Ok(RegistryReference {
                            name,
                            subject: &info.subject,
                            version: info.version,
                        })
                    })
                    .collect::<Result<Vec<_>>>()?;
            let version = register_schema(
                sr_settings,
                &subject,
                &descriptor.fullname,
                &descriptor.content,
                references,
            )
            .await
            .with_context(|| {
                format!("failed registering Avro schema {:?}", descriptor.path)
            })?;

            cache.insert(
                descriptor.fullname.clone(),
                descriptor.fullname.clone(),
            );
            cache.insert(
                descriptor.path.to_string_lossy().into_owned(),
                descriptor.fullname.clone(),
            );
            if let Some(filename) =
                descriptor.path.file_name().and_then(|file| file.to_str())
            {
                cache.insert(filename.to_owned(), descriptor.fullname.clone());
            }
            tracing::info!(
                event.name = "schema.registry.registered",
                path = ?descriptor.path,
                fullname = %descriptor.fullname,
                dependencies = ?descriptor.dependencies,
                version,
                "schema registered and resolved"
            );
            let dependencies = descriptor.dependencies;
            registered_schemas.insert(
                descriptor.fullname,
                RegisteredInfo {
                    subject,
                    content: descriptor.content,
                    version,
                    dependencies,
                },
            );
        }

        Ok(cache)
    }
}

fn append_compilation_dependency<'a>(
    name: &str,
    registered: &'a HashMap<String, RegisteredInfo>,
    included: &mut HashSet<String>,
    context: &mut Vec<&'a str>,
) -> Result<()> {
    if !included.insert(name.to_string()) {
        return Ok(());
    }
    let info = registered.get(name).ok_or_else(|| {
        anyhow!("schema dependency {name} was not registered")
    })?;
    for dependency in &info.dependencies {
        append_compilation_dependency(
            dependency, registered, included, context,
        )?;
    }
    context.push(&info.content);
    Ok(())
}

fn schema_registration_plan(
    raw_schemas: Vec<(PathBuf, String)>,
) -> Result<Vec<SchemaDescriptor>> {
    let mut parsed = Vec::with_capacity(raw_schemas.len());
    let mut root_names = HashSet::with_capacity(raw_schemas.len());
    for (path, content) in raw_schemas {
        let value: serde_json::Value = parse_json(&content, &path)?;
        let object = value.as_object().ok_or_else(|| {
            anyhow!("Avro schema {:?} must be an object", path)
        })?;
        if object.get("type").and_then(serde_json::Value::as_str) !=
            Some("record")
        {
            bail!("avro root inside {:?} must be a record", path);
        }
        let name = required_string(object.get("name"), "name", &path)?;
        let namespace =
            required_string(object.get("namespace"), "namespace", &path)?;
        let fullname = format!("{namespace}.{name}");
        if !root_names.insert(fullname.clone()) {
            bail!("duplicate Avro root name {fullname}");
        }
        parsed.push((path, content, value, fullname));
    }

    let mut descriptors = Vec::with_capacity(parsed.len());
    for (path, content, value, fullname) in parsed {
        let mut referenced_names = BTreeSet::new();
        collect_fullname_references(&value, &mut referenced_names);
        referenced_names.remove(&fullname);
        for reference in &referenced_names {
            if !root_names.contains(reference) {
                bail!("schema {:?} references missing root {reference}", path);
            }
        }
        descriptors.push(SchemaDescriptor {
            path,
            content,
            fullname,
            dependencies: referenced_names.into_iter().collect(),
        });
    }

    descriptors
        .sort_unstable_by(|left, right| left.fullname.cmp(&right.fullname));
    let mut ordered = Vec::with_capacity(descriptors.len());
    let mut registered = HashSet::with_capacity(descriptors.len());
    while !descriptors.is_empty() {
        let candidate = descriptors.iter().position(|descriptor| {
            descriptor
                .dependencies
                .iter()
                .all(|name| registered.contains(name))
        });
        let Some(index) = candidate else {
            let names: Vec<&str> = descriptors
                .iter()
                .map(|descriptor| descriptor.fullname.as_str())
                .collect();
            bail!("circular Avro schema dependencies: {names:?}");
        };
        let descriptor = descriptors.remove(index);
        registered.insert(descriptor.fullname.clone());
        ordered.push(descriptor);
    }
    Ok(ordered)
}

fn parse_json<T: DeserializeOwned>(
    content: &str,
    path: &PathBuf,
) -> Result<T> {
    serde_json::from_str(content)
        .with_context(|| format!("failed parsing Avro JSON at {path:?}"))
}

fn required_string<'a>(
    value: Option<&'a serde_json::Value>,
    field: &str,
    path: &PathBuf,
) -> Result<&'a str> {
    value
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            anyhow!("Avro schema {:?} requires string field '{field}'", path)
        })
}

fn collect_fullname_references(
    value: &serde_json::Value,
    references: &mut BTreeSet<String>,
) {
    match value {
        serde_json::Value::String(candidate)
            if is_record_fullname(candidate) =>
        {
            references.insert(candidate.clone());
        },
        serde_json::Value::Array(items) => {
            for item in items {
                collect_fullname_references(item, references);
            }
        },
        serde_json::Value::Object(object) => {
            for value in object.values() {
                collect_fullname_references(value, references);
            }
        },
        _ => {},
    }
}

fn is_record_fullname(value: &str) -> bool {
    value
        .strip_prefix("com.galadril.")
        .and_then(|suffix| suffix.rsplit('.').next())
        .and_then(|name| name.chars().next())
        .is_some_and(char::is_uppercase)
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

        let trace_carrier = current_w3c_carrier();
        let mut headers = OwnedHeaders::new();
        for (header_key, header_value) in
            trace_carrier.entries().into_iter().flatten()
        {
            headers = headers.insert(Header {
                key: header_key,
                value: Some(header_value),
            });
        }
        let record = FutureRecord::to(topic)
            .key(key)
            .payload(&encoded)
            .headers(headers);

        self.producer
            .send(record, Duration::from_secs(5))
            .await
            .map_err(|(err, _)| anyhow!("kafka transfer failure: {err:?}"))?;

        tracing::debug!(
            event.name = "kafka.event.published",
            %topic,
            traceparent = trace_carrier.get("traceparent"),
            "event published"
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::Mutex;

    use super::*;

    type PublishedCall = (String, Option<String>, String, serde_json::Value);
    type PublishedCalls = Vec<PublishedCall>;

    #[test]
    fn bundled_schemas_resolve_with_registry_reference_semantics() -> Result<()>
    {
        let schema_dir = bundled_schema_dir()?.canonicalize()?;
        let mut pending = Vec::new();
        for entry in fs::read_dir(&schema_dir)? {
            let entry = entry?;
            let path = entry.path();
            let path_canonical = match path.canonicalize() {
                Ok(p) => p,
                Err(_) => continue,
            };
            if !path_canonical.starts_with(&schema_dir) {
                continue;
            }
            if path_canonical
                .extension()
                .is_some_and(|extension| extension == "avsc")
            {
                let content = fs::read_to_string(&path_canonical)?;
                pending.push((path_canonical, content));
            }
        }
        pending.reverse();
        let plan = schema_registration_plan(pending)?;
        let mut compiled = HashMap::new();
        for descriptor in &plan {
            let mut context = Vec::new();
            let mut included = HashSet::new();
            for dependency in &descriptor.dependencies {
                append_compilation_dependency(
                    dependency,
                    &compiled,
                    &mut included,
                    &mut context,
                )?;
            }
            context.push(&descriptor.content);
            apache_avro::Schema::parse_list(&context)?;
            compiled.insert(
                descriptor.fullname.clone(),
                RegisteredInfo {
                    subject: format!("{}-value", descriptor.fullname),
                    content: descriptor.content.clone(),
                    version: 1,
                    dependencies: descriptor.dependencies.clone(),
                },
            );
        }
        let positions: HashMap<&str, usize> = plan
            .iter()
            .enumerate()
            .map(|(index, descriptor)| (descriptor.fullname.as_str(), index))
            .collect();
        let authz = positions["com.galadril.auth.Authz"];
        let observation =
            positions["com.galadril.observation.ObservationContext"];
        let manifest = positions["com.galadril.ingest.IngestionManifest"];
        assert!(authz < manifest);
        assert!(observation < manifest);
        assert!(authz < positions["com.galadril.raw.Video"]);
        assert!(observation < positions["com.galadril.raw.Sensor"]);
        Ok(())
    }

    fn bundled_schema_dir() -> Result<PathBuf> {
        let mut candidates = vec![
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../schemas/avro"),
        ];
        if let (Ok(test_srcdir), Ok(test_workspace)) = (
            std::env::var("TEST_SRCDIR"),
            std::env::var("TEST_WORKSPACE"),
        ) {
            candidates.push(
                PathBuf::from(test_srcdir)
                    .join(test_workspace)
                    .join("schemas/avro"),
            );
        }
        if let (Ok(runfiles_dir), Ok(test_workspace)) = (
            std::env::var("RUNFILES_DIR"),
            std::env::var("TEST_WORKSPACE"),
        ) {
            candidates.push(
                PathBuf::from(runfiles_dir)
                    .join(test_workspace)
                    .join("schemas/avro"),
            );
        }

        candidates
            .iter()
            .find(|path| path.is_dir())
            .cloned()
            .ok_or_else(|| {
                anyhow!(
                    "bundled Avro schema directory is absent; checked {candidates:?}"
                )
            })
    }

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
    fn dependency_plan_does_not_depend_on_filenames() -> Result<()> {
        let schemas = vec![
            (
                PathBuf::from("first.avsc"),
                r#"{"type":"record","name":"Child","namespace":"com.galadril.test","fields":[{"name":"parent","type":"com.galadril.test.Parent"}]}"#.to_string(),
            ),
            (
                PathBuf::from("last.avsc"),
                r#"{"type":"record","name":"Parent","namespace":"com.galadril.test","fields":[]}"#.to_string(),
            ),
        ];
        let plan = schema_registration_plan(schemas)?;
        assert_eq!(plan[0].fullname, "com.galadril.test.Parent");
        assert_eq!(plan[1].fullname, "com.galadril.test.Child");
        Ok(())
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
            assert!(err.contains("references missing root"));
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
