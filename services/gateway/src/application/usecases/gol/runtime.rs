//! Worker transport carries intent; all authority remains in the host.

use std::collections::BTreeMap;
use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::receipts::CommandHandoff;

/// Payload-free errors prevent disclosure of private dependency metadata.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RuntimeError {
    Invalid,
    Denied,
    Unavailable,
}

impl fmt::Display for RuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("GOL operation unavailable")
    }
}
impl std::error::Error for RuntimeError {}

/// Authentication, classifications and pins cannot be supplied in this body.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct RuntimeRequest {
    pub tenant_id: String,
    pub pipeline_id: String,
    pub pipeline_revision_id: String,
    pub block_id: String,
    pub source_object_id: String,
    pub admission: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct ResumeRequest {
    pub tenant_id: String,
    pub pipeline_id: String,
    pub pipeline_revision_id: String,
    pub block_id: String,
    pub source_object_id: String,
    pub command_id: uuid::Uuid,
    pub lineage: String,
}

#[derive(Deserialize)]
#[serde(untagged)]
pub(crate) enum RuntimeOpenRequest {
    Open(RuntimeRequest),
    Resume(ResumeRequest),
}

#[derive(Serialize)]
pub(crate) struct ResumeResponse {
    pub admission: String,
}

/// Every operation in a transaction is checked before any effect begins.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct RequestedEffect {
    pub operation: String,
    pub target: String,
    pub destination: String,
    pub purpose: String,
    pub instance_id: Option<String>,
    pub proposal: Option<Value>,
    pub receipt: Option<String>,
    pub(super) handoff: Option<CommandHandoff>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct BeginEffects {
    pub tenant_id: String,
    pub pipeline_id: String,
    pub pipeline_revision_id: String,
    pub block_id: String,
    pub source_object_id: String,
    pub admission: String,
    pub context_id: String,
    pub generation_id: String,
    pub effects: Vec<RequestedEffect>,
}

/// Completion can only release the boundary of its original calculation.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct CompleteEffects {
    pub tenant_id: String,
    pub pipeline_id: String,
    pub pipeline_revision_id: String,
    pub block_id: String,
    pub source_object_id: String,
    pub admission: String,
    pub context_id: String,
    pub generation_id: String,
    pub completion: String,
    pub succeeded: bool,
}

#[derive(Deserialize)]
#[serde(untagged)]
pub(crate) enum EffectRequest {
    Begin(BeginEffects),
    Complete(CompleteEffects),
}

/// Only admitted metadata crosses the worker boundary.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct RuntimeSymbol {
    pub key: String,
    pub name: String,
    pub kind: String,
    pub version: String,
    pub references: Vec<String>,
    pub properties: BTreeMap<String, Value>,
    pub roles: BTreeMap<String, String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct RuntimeLease {
    pub tenant_id: String,
    pub ontology_id: String,
    pub ontology_version: String,
    pub source_revision: String,
    pub generation_id: String,
    pub context_id: String,
    pub symbols: Vec<RuntimeSymbol>,
    pub schemas: BTreeMap<String, Value>,
}

/// A permit retains a live exclusion boundary, not reusable permission.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct EffectResponse {
    pub allowed: bool,
    pub permit: Option<String>,
    pub receipts: Vec<Option<String>>,
}
