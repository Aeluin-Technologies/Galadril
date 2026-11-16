//! Authenticate durable IFC provenance without persisting authorization
//! decisions.

use std::collections::BTreeSet;
use std::io::{self, Write};

use anyhow::{Context, Result, ensure};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use galadril_ontology::Reference;
use galadril_ontology::archive::remote::Generation;
use galadril_ontology::authorization::{Flow, Operation};
use ring::hmac;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

const MAX_VALUE_BYTES: usize = 128 * 1024;
const MAX_RECEIPT_BYTES: usize = 16 * 1024;

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

pub(super) struct ReceiptScope<'a> {
    pub tenant: &'a str,
    pub target: &'a Reference,
    pub instance_id: &'a str,
    pub generation: &'a str,
    pub source_revision: &'a str,
    pub dependencies: &'a BTreeSet<Dependency>,
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
    pub dependencies: BTreeSet<Dependency>,
    value_sha256: [u8; 32],
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
                scope.source_revision.len() <= 128,
            "Invalid GOL provenance scope"
        );
        scope.generation.parse::<Generation>()?;
        let receipt = LabelReceipt {
            format: 2,
            tenant: scope.tenant.to_owned(),
            target: scope.target.clone(),
            instance_id: scope.instance_id.to_owned(),
            generation: scope.generation.to_owned(),
            source_revision: scope.source_revision.to_owned(),
            flow: flow.clone(),
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
        ensure!(
            token.len() <= 2 * MAX_RECEIPT_BYTES,
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
            receipt.format == 2 &&
                receipt.tenant == tenant &&
                receipt.instance_id == instance_id &&
                receipt.value_sha256 == value_hash(value)?,
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
