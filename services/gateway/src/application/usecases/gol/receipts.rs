//! Authenticate durable IFC provenance without persisting authorization
//! decisions.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Write};

use anyhow::{Context, Result, ensure};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use galadril_ontology::Reference;
use galadril_ontology::archive::remote::Generation;
use galadril_ontology::authorization::{Flow, Operation};
use ring::hmac;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;
use sha2::{Digest, Sha256};
use uuid::Uuid;

const MAX_VALUE_BYTES: usize = 128 * 1024;
const MAX_RECEIPT_TOKEN_BYTES: usize = 64 * 1024;
// The worker accepts 64 KiB on the wire, including the 43-byte encoded
// SHA-256 tag and its separator.
const MAX_RECEIPT_BYTES: usize = (MAX_RECEIPT_TOKEN_BYTES - 44) / 4 * 3;
const LEGACY_MAX_DEPENDENCIES: usize = 1024;
pub(super) const MAX_DEPENDENCIES: usize = 4096;

/// Persisted provenance retains revocable obligations, rather than decisions.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Dependency {
    pub ontology: String,
    pub target: String,
    pub instance_id: String,
    pub revision: String,
    pub operation: Operation,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct IndexedDependencies<T> {
    strings: Vec<T>,
    rows: Vec<(u16, u16, u16, u16, Operation)>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct GroupedDependencies<T> {
    strings: Vec<T>,
    groups: Vec<([u16; 3], Operation, Vec<u16>)>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum DependenciesWire {
    Grouped(GroupedDependencies<String>),
    Indexed(IndexedDependencies<String>),
    Legacy(BTreeSet<Dependency>),
}

/// Interns repeated fields without dropping the individual live obligations.
fn serialize_dependencies<S: Serializer>(
    dependencies: &BTreeSet<Dependency>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    let mut keys = BTreeMap::new();
    let mut strings = Vec::new();
    let mut groups = BTreeMap::<([u16; 3], Operation), Vec<u16>>::new();
    let mut index = |value| match keys.entry(value) {
        Entry::Occupied(entry) => Ok(*entry.get()),
        Entry::Vacant(entry) => {
            let id = u16::try_from(strings.len())
                .map_err(serde::ser::Error::custom)?;
            strings.push(value);
            Ok(*entry.insert(id))
        },
    };
    for dependency in dependencies {
        let scope = [
            index(dependency.ontology.as_str())?,
            index(dependency.instance_id.as_str())?,
            index(dependency.revision.as_str())?,
        ];
        groups
            .entry((scope, dependency.operation))
            .or_default()
            .push(index(dependency.target.as_str())?);
    }
    GroupedDependencies {
        strings,
        groups: groups
            .into_iter()
            .map(|((scope, operation), targets)| (scope, operation, targets))
            .collect(),
    }
    .serialize(serializer)
}

fn deserialize_dependencies<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<BTreeSet<Dependency>, D::Error> {
    let dependency = |strings: &[String],
                      ontology,
                      target,
                      instance,
                      revision,
                      operation| {
        let read = |key| {
            strings.get(usize::from(key)).cloned().ok_or_else(|| {
                serde::de::Error::custom("Invalid GOL dependency reference")
            })
        };
        Ok(Dependency {
            ontology: read(ontology)?,
            target: read(target)?,
            instance_id: read(instance)?,
            revision: read(revision)?,
            operation,
        })
    };
    match DependenciesWire::deserialize(deserializer)? {
        DependenciesWire::Grouped(grouped)
            if grouped.groups.len() <= MAX_DEPENDENCIES &&
                grouped
                    .groups
                    .iter()
                    .all(|(_, _, targets)| !targets.is_empty()) &&
                grouped
                    .groups
                    .iter()
                    .map(|(_, _, targets)| targets.len())
                    .sum::<usize>() <=
                    MAX_DEPENDENCIES &&
                grouped.strings.len() <= 4 * MAX_DEPENDENCIES =>
        {
            let mut dependencies = BTreeSet::new();
            for ([ontology, instance, revision], operation, targets) in
                grouped.groups
            {
                for target in targets {
                    dependencies.insert(dependency(
                        &grouped.strings,
                        ontology,
                        target,
                        instance,
                        revision,
                        operation,
                    )?);
                }
            }
            Ok(dependencies)
        },
        DependenciesWire::Legacy(dependencies)
            if dependencies.len() <= LEGACY_MAX_DEPENDENCIES =>
        {
            Ok(dependencies)
        },
        DependenciesWire::Indexed(indexed)
            if indexed.rows.len() <= LEGACY_MAX_DEPENDENCIES &&
                indexed.strings.len() <= 4 * indexed.rows.len() =>
        {
            indexed
                .rows
                .into_iter()
                .map(|(ontology, target, instance, revision, operation)| {
                    dependency(
                        &indexed.strings,
                        ontology,
                        target,
                        instance,
                        revision,
                        operation,
                    )
                })
                .collect()
        },
        _ => Err(serde::de::Error::custom(
            "GOL provenance exceeds dependency limit",
        )),
    }
}

pub(super) struct ReceiptScope<'a> {
    pub tenant: &'a str,
    pub target: &'a Reference,
    pub instance_id: &'a str,
    pub generation: &'a str,
    pub source_revision: &'a str,
    pub dependencies: &'a BTreeSet<Dependency>,
    pub lineage: Option<&'a LineageOrigin>,
}

/// Binds a broker lineage proof to one already-routed command.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct CommandHandoff {
    pub block_id: String,
    pub command_id: Uuid,
}

/// Authenticates the origin needed to request a new calculation after restart.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct LineageOrigin {
    pub issuer: String,
    pub subject_id: String,
    pub source_object_id: String,
    pub source_revision: String,
    pub source_marker: [u8; 32],
    pub pipeline_id: String,
    pub pipeline_revision_id: String,
    pub pins: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub handoff: Option<CommandHandoff>,
}

/// The signature authenticates a label and value binding, never an Allow.
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct LabelReceipt {
    format: u8,
    pub tenant: String,
    pub target: Reference,
    pub instance_id: String,
    pub generation: String,
    pub source_revision: String,
    pub flow: Flow,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lineage: Option<LineageOrigin>,
    #[serde(
        serialize_with = "serialize_dependencies",
        deserialize_with = "deserialize_dependencies"
    )]
    pub dependencies: BTreeSet<Dependency>,
    value_sha256: [u8; 32],
}

impl LabelReceipt {
    /// Broker proofs preserve the recipient selected at their original
    /// release.
    pub fn verify_command(&self, block: &str, command_id: Uuid) -> Result<()> {
        if let Some(handoff) = self
            .lineage
            .as_ref()
            .and_then(|origin| origin.handoff.as_ref())
        {
            ensure!(
                handoff.block_id == block && handoff.command_id == command_id,
                "GOL lineage scope rejected"
            );
        }
        Ok(())
    }
}

pub(super) struct ReceiptSigner {
    key: hmac::Key,
}

impl ReceiptSigner {
    /// A deployment-owned key is required; no key is derived from public pins.
    pub fn new(key: &[u8]) -> Result<Self> {
        ensure!((32..=64).contains(&key.len()), "Invalid GOL provenance key");
        Ok(Self {
            key: hmac::Key::new(hmac::HMAC_SHA256, key),
        })
    }

    /// Binds the complete proposed value after live GOL checks have succeeded.
    /// The caller must preserve the label on errors and incomplete effects
    /// too.
    pub fn issue(
        &self,
        scope: &ReceiptScope<'_>,
        flow: &Flow,
        value: &Value,
    ) -> Result<String> {
        ensure!(
            !scope.tenant.is_empty() &&
                scope.tenant.len() <= 128 &&
                !scope.instance_id.is_empty() &&
                scope.instance_id.len() <= 1024 &&
                !scope.source_revision.is_empty() &&
                scope.source_revision.len() <= 128 &&
                scope.dependencies.len() <= MAX_DEPENDENCIES,
            "Invalid GOL provenance scope"
        );
        scope.generation.parse::<Generation>()?;
        let receipt = LabelReceipt {
            format: 4,
            tenant: scope.tenant.to_owned(),
            target: scope.target.clone(),
            instance_id: scope.instance_id.to_owned(),
            generation: scope.generation.to_owned(),
            source_revision: scope.source_revision.to_owned(),
            flow: flow.clone(),
            lineage: scope.lineage.cloned(),
            dependencies: scope.dependencies.clone(),
            value_sha256: value_hash(value)?,
        };
        let bytes = serde_json::to_vec(&receipt)?;
        ensure!(
            bytes.len() <= MAX_RECEIPT_BYTES,
            "GOL provenance exceeds limit"
        );
        let tag = hmac::sign(&self.key, &bytes);
        Ok(format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(bytes),
            URL_SAFE_NO_PAD.encode(tag.as_ref())
        ))
    }

    /// A valid receipt only supplies a floor. The reader must still resolve
    /// the active contract and check current permissions, context and
    /// destination.
    pub fn verify(
        &self,
        token: &str,
        tenant: &str,
        instance_id: &str,
        value: &Value,
    ) -> Result<LabelReceipt> {
        let receipt = self.authenticate(token, tenant)?;
        ensure!(
            receipt
                .lineage
                .as_ref()
                .is_none_or(|origin| origin.handoff.is_none()) &&
                receipt.instance_id == instance_id &&
                receipt.value_sha256 == value_hash(value)?,
            "GOL provenance unavailable"
        );
        Ok(receipt)
    }

    /// Certifies provenance for re-admission. Reading the value still needs
    /// the exact proposal, its receipt, and a live read permit.
    pub fn verify_lineage(
        &self,
        token: &str,
        tenant: &str,
        source: &str,
        pipeline: &str,
        revision: &str,
    ) -> Result<LabelReceipt> {
        let receipt = self.authenticate(token, tenant)?;
        let origin = receipt
            .lineage
            .as_ref()
            .context("GOL lineage unavailable")?;
        ensure!(
            origin.source_object_id == source &&
                origin.pipeline_id == pipeline &&
                origin.pipeline_revision_id == revision,
            "GOL lineage scope rejected"
        );
        Ok(receipt)
    }

    fn authenticate(&self, token: &str, tenant: &str) -> Result<LabelReceipt> {
        ensure!(
            token.len() <= MAX_RECEIPT_TOKEN_BYTES,
            "GOL provenance unavailable"
        );
        let (payload, signature) = token
            .split_once('.')
            .context("GOL provenance unavailable")?;
        let bytes = URL_SAFE_NO_PAD
            .decode(payload)
            .context("GOL provenance unavailable")?;
        let tag = URL_SAFE_NO_PAD
            .decode(signature)
            .context("GOL provenance unavailable")?;
        ensure!(
            bytes.len() <= MAX_RECEIPT_BYTES,
            "GOL provenance unavailable"
        );
        hmac::verify(&self.key, &bytes, &tag)
            .map_err(|_| anyhow::anyhow!("GOL provenance unavailable"))?;
        let receipt: LabelReceipt = serde_json::from_slice(&bytes)
            .context("GOL provenance unavailable")?;
        ensure!(
            matches!(receipt.format, 2..=4) && receipt.tenant == tenant,
            "GOL provenance unavailable"
        );
        receipt.generation.parse::<Generation>()?;
        Ok(receipt)
    }
}

struct Counter(usize);

impl Write for Counter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0 = self
            .0
            .checked_add(bytes.len())
            .filter(|size| *size <= MAX_VALUE_BYTES)
            .ok_or_else(|| io::Error::other("GOL value exceeds limit"))?;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn value_hash(value: &Value) -> Result<[u8; 32]> {
    let mut digest = Sha256::new();
    let mut nodes = 0;
    hash_node(value, &mut digest, 0, &mut nodes)?;
    serde_json::to_writer(Counter(0), value)
        .context("GOL value exceeds limit")?;
    Ok(digest.finalize().into())
}

fn hash_node(
    value: &Value,
    digest: &mut Sha256,
    depth: usize,
    nodes: &mut usize,
) -> Result<()> {
    *nodes = nodes.checked_add(1).context("GOL value exceeds limit")?;
    ensure!(depth <= 32 && *nodes <= 8192, "GOL value exceeds limit");
    match value {
        Value::Null => digest.update(b"n"),
        Value::Bool(value) => digest.update(if *value { b"t" } else { b"f" }),
        Value::Number(value) => {
            digest.update(b"d");
            hash_bytes(digest, value.to_string().as_bytes())?;
        },
        Value::String(value) => {
            digest.update(b"s");
            hash_bytes(digest, value.as_bytes())?;
        },
        Value::Array(values) => {
            digest.update(b"a");
            digest.update(u64::try_from(values.len())?.to_le_bytes());
            for value in values {
                hash_node(value, digest, depth + 1, nodes)?;
            }
        },
        Value::Object(values) => {
            ensure!(values.len() <= 1024, "GOL value exceeds limit");
            digest.update(b"o");
            digest.update(u64::try_from(values.len())?.to_le_bytes());
            // Borrowing sorted keys avoids cloning potentially large values
            // and remains deterministic if serde_json enables
            // insertion ordering.
            let mut entries: Vec<_> = values.iter().collect();
            entries.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
            for (name, value) in entries {
                hash_bytes(digest, name.as_bytes())?;
                hash_node(value, digest, depth + 1, nodes)?;
            }
        },
    }
    Ok(())
}

fn hash_bytes(digest: &mut Sha256, bytes: &[u8]) -> Result<()> {
    ensure!(bytes.len() <= MAX_VALUE_BYTES, "GOL value exceeds limit");
    digest.update(u64::try_from(bytes.len())?.to_le_bytes());
    digest.update(bytes);
    Ok(())
}
