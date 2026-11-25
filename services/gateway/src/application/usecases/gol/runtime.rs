//! Worker transport carries intent; all authority remains in the host.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use anyhow::{Context, Result, ensure};
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
    pub metadata_target: Option<String>,
}

/// A projection can only narrow the immutable binding selected by Registry.
pub(super) fn worker_metadata_targets<'a>(
    binding: &'a BTreeSet<String>,
    requested: Option<&str>,
) -> Result<impl Iterator<Item = &'a String> + use<'a>> {
    let target = requested
        .map(|key| binding.get(key).context("GOL metadata target unavailable"))
        .transpose()?;
    Ok(binding
        .iter()
        .filter(move |key| target.is_none_or(|target| target == *key)))
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
    pub(super) read_fields: Option<Vec<String>>,
}

impl RequestedEffect {
    /// Selects fields only after the caller treats the whole proposal as
    /// signed input.
    pub(super) fn read_fields(&self) -> Result<Vec<&str>> {
        let value = self
            .proposal
            .as_ref()
            .and_then(Value::as_object)
            .context("GOL record provenance required")?;
        let Some(fields) = &self.read_fields else {
            return Ok(value.keys().map(String::as_str).collect());
        };
        ensure!(
            self.operation == "read" &&
                self.receipt.is_some() &&
                !fields.is_empty() &&
                fields.len() <= 128 &&
                fields.iter().collect::<BTreeSet<_>>().len() ==
                    fields.len() &&
                fields.iter().all(|field| value.contains_key(field)),
            "GOL read projection unavailable"
        );
        Ok(fields.iter().map(String::as_str).collect())
    }
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
    #[serde(default)]
    pub select_readable: bool,
}

impl BeginEffects {
    /// Selective admission is defined only for signed, concrete reads.
    pub(super) fn validate_read_selection(&self) -> Result<()> {
        ensure!(
            !self.select_readable ||
                self.effects.iter().all(|effect| {
                    effect.operation == "read" &&
                        effect.instance_id.is_some() &&
                        effect.proposal.is_some() &&
                        effect.receipt.is_some() &&
                        effect.handoff.is_none()
                }),
            "GOL read selection requires signed values"
        );
        Ok(())
    }
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
    #[serde(rename = "allowedReads", skip_serializing_if = "Option::is_none")]
    pub allowed_reads: Option<Vec<bool>>,
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn worker_metadata_projection_cannot_escape_the_declared_binding()
    -> Result<()> {
        let declared =
            BTreeSet::from(["mail::Message".into(), "mail::Thread".into()]);
        assert_eq!(
            worker_metadata_targets(&declared, Some("mail::Message"))?
                .map(String::as_str)
                .collect::<Vec<_>>(),
            ["mail::Message"]
        );
        assert!(
            worker_metadata_targets(&declared, Some("other::Message"))
                .is_err()
        );
        assert_eq!(worker_metadata_targets(&declared, None)?.count(), 2);
        Ok(())
    }

    #[test]
    fn read_selection_requires_only_concrete_signed_reads() -> Result<()> {
        let wire = json!({
            "tenantId": "tenant", "pipelineId": "pipeline",
            "pipelineRevisionId": "revision", "blockId": "block",
            "sourceObjectId": "raw:tenant/key", "admission": "admission",
            "contextId": "context", "generationId": "generation",
            "selectReadable": true,
            "effects": [{
                "operation": "read", "target": "example::Prototype",
                "destination": "identity_state", "purpose": "identity_resolution",
                "instanceId": "entity", "proposal": {"embedding": [1.0]},
                "receipt": "signed-prototype"
            }]
        });
        let mut batch: BeginEffects = serde_json::from_value(wire)?;
        assert!(batch.select_readable);
        batch.validate_read_selection()?;
        let effect = batch.effects.first_mut().context("missing effect")?;
        effect.operation = "write".into();
        assert!(batch.validate_read_selection().is_err());
        let effect = batch.effects.first_mut().context("missing effect")?;
        effect.operation = "read".into();
        effect.receipt = None;
        assert!(batch.validate_read_selection().is_err());
        Ok(())
    }

    #[test]
    fn signed_projection_selects_only_existing_fields_of_a_read()
    -> anyhow::Result<()> {
        let mut effect: RequestedEffect = serde_json::from_value(json!({
            "operation": "read",
            "target": "example::Observation",
            "destination": "causal_state",
            "purpose": "causal_analysis",
            "instanceId": "observation-1",
            "proposal": {"time": "now", "measurement": 1.5, "private_note": "secret"},
            "receipt": "signed-value",
            "readFields": ["time", "measurement"]
        }))?;
        assert_eq!(effect.read_fields()?, ["time", "measurement"]);
        effect.read_fields = Some(vec!["unknown".into()]);
        assert!(effect.read_fields().is_err());
        effect.read_fields = Some(vec!["time".into(), "time".into()]);
        assert!(effect.read_fields().is_err());
        effect.read_fields = Some(vec![]);
        assert!(effect.read_fields().is_err());
        effect.read_fields = Some(vec!["time".into()]);
        effect.operation = "write".into();
        assert!(effect.read_fields().is_err());
        effect.operation = "read".into();
        effect.receipt = None;
        assert!(effect.read_fields().is_err());
        Ok(())
    }
}
