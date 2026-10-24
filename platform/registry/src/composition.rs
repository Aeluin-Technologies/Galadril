//! Immutable, bounded expansion of tenant-local pipeline transfers.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::semantics::validate_pipeline_structure;
use crate::storage::valid_revision;

/// Includes source blocks and every reused occurrence, not just unique graphs.
pub const MAX_BLOCKS: usize = 256;
/// Limits cross-pipeline transfers independently of ordinary local branches.
pub const MAX_TRANSFERS: usize = 3;

/// Exact immutable input selected by an authorized pipeline author.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PipelineReference {
    /// Tenant-local pipeline; tenant identity never comes from this
    /// reference.
    pub pipeline_id: String,
    /// Exact Registry commit selected for this transfer.
    pub revision_id: String,
    /// Receiving source contract; no external ingress is re-triggered.
    pub input_source: String,
}

fn identity(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 128 &&
        value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
}

/// Rejects reserved compiler fields before reading any referenced artifact.
pub fn references(definition: &Value) -> Result<Vec<PipelineReference>> {
    let sources = definition
        .get("sources")
        .and_then(Value::as_array)
        .context("Pipeline sources missing")?;
    let steps = definition
        .get("pipeline")
        .and_then(Value::as_array)
        .context("Pipeline steps missing")?;
    ensure!(
        sources.len() + steps.len() <= MAX_BLOCKS,
        "Pipeline exceeds 256 blocks"
    );
    let mut result = Vec::new();
    for node in sources.iter().chain(steps) {
        let id = node
            .get("step")
            .or_else(|| node.get("id"))
            .and_then(Value::as_str)
            .context("Pipeline block identifier missing")?;
        ensure!(
            !id.starts_with("__linked_") &&
                node.get("origin").is_none_or(Value::is_null),
            "Reserved compiled pipeline field"
        );
        if node.get("type").and_then(Value::as_str) == Some("pipeline") {
            ensure!(
                node.get("capability_id").is_none_or(Value::is_null) &&
                    node.get("model").is_none_or(Value::is_null),
                "Native transfers cannot select plugin code or a model"
            );
            let reference: PipelineReference = serde_json::from_value(
                node.get("pipeline_ref")
                    .context("Pipeline reference missing")?
                    .clone(),
            )?;
            ensure!(
                identity(&reference.pipeline_id) &&
                    valid_revision(&reference.revision_id) &&
                    identity(&reference.input_source),
                "Invalid pipeline reference"
            );
            ensure!(
                node.get("params").is_none_or(|p| p
                    .get("trigger")
                    .is_none_or(|v| v == "manual")),
                "Pipeline transfers must be event driven"
            );
            ensure!(
                !steps.iter().any(|step| step
                    .get("input_from")
                    .and_then(Value::as_array)
                    .is_some_and(|inputs| inputs
                        .iter()
                        .any(|input| input.as_str() == Some(id)))),
                "Pipeline transfers are terminal; continuations require explicit return semantics"
            );
            result.push(reference);
            ensure!(
                result.len() <= MAX_TRANSFERS,
                "Composition exceeds three pipeline transfers"
            );
        } else {
            ensure!(
                node.get("pipeline_ref").is_none_or(Value::is_null) &&
                    node.get("type").and_then(Value::as_str) !=
                        Some("forward"),
                "Pipeline references require a transfer block"
            );
        }
    }
    Ok(result)
}

struct Expansion<'a> {
    definitions: &'a BTreeMap<(String, String), Value>,
    active: BTreeSet<String>,
    steps: Vec<Value>,
    blocks: usize,
    transfers: usize,
    next_id: usize,
}

impl Expansion<'_> {
    fn append(
        &mut self,
        pipeline_id: &str,
        revision_id: &str,
        definition: &Value,
        entry: Option<(&str, &str)>,
    ) -> Result<()> {
        ensure!(
            self.active.insert(pipeline_id.to_owned()),
            "Cross-pipeline cycle detected"
        );
        self.transfers += references(definition)?.len();
        ensure!(
            self.transfers <= MAX_TRANSFERS,
            "Composition exceeds three pipeline transfers"
        );
        let order = validate_pipeline_structure(definition)?;
        let sources = definition
            .get("sources")
            .and_then(Value::as_array)
            .context("Pipeline sources missing")?;
        let steps = definition
            .get("pipeline")
            .and_then(Value::as_array)
            .context("Pipeline steps missing")?;
        self.blocks += sources.len() + steps.len();
        ensure!(
            self.blocks <= MAX_BLOCKS,
            "Expanded composition exceeds 256 blocks"
        );
        let mut reachable = BTreeSet::new();
        if let Some((selected, _)) = entry {
            ensure!(
                sources
                    .iter()
                    .any(|source| source.get("id").and_then(Value::as_str) ==
                        Some(selected)),
                "Selected pipeline input is unavailable"
            );
            reachable.insert(selected.to_owned());
            for id in order {
                if let Some(step) = steps.iter().find(|step| {
                    step.get("step").and_then(Value::as_str) == Some(&id)
                }) && !matches!(
                    step.pointer("/params/trigger").and_then(Value::as_str),
                    Some("cron" | "on_demand")
                ) {
                    let inputs = step
                        .get("input_from")
                        .and_then(Value::as_array)
                        .context("Step inputs missing")?;
                    if inputs.iter().any(|input| {
                        input.as_str().is_some_and(|id| reachable.contains(id))
                    }) {
                        ensure!(
                            inputs.len() == 1,
                            "Linked inputs cannot create an implicit stateful join"
                        );
                        reachable.insert(id);
                    }
                }
            }
        }
        let mut names = BTreeMap::new();
        for source in sources {
            let id = source
                .get("id")
                .and_then(Value::as_str)
                .context("Source identifier missing")?;
            let mapped = if let Some((input_source, transfer)) = entry {
                if id != input_source {
                    continue;
                }
                transfer.to_owned()
            } else {
                id.to_owned()
            };
            names.insert(id.to_owned(), mapped);
        }
        for step in steps {
            let id = step
                .get("step")
                .and_then(Value::as_str)
                .context("Step identifier missing")?;
            if entry.is_some() && !reachable.contains(id) {
                continue;
            }
            let mapped = if entry.is_some() {
                self.next_id += 1;
                format!("__linked_{}", self.next_id)
            } else {
                id.to_owned()
            };
            names.insert(id.to_owned(), mapped);
        }
        for step in steps {
            let mut expanded = step.clone();
            let id = step
                .get("step")
                .and_then(Value::as_str)
                .context("Step identifier missing")?;
            if entry.is_some() && !reachable.contains(id) {
                continue;
            }
            let mapped = names.get(id).context("Unknown step")?.clone();
            let inputs = step
                .get("input_from")
                .and_then(Value::as_array)
                .context("Step inputs missing")?;
            let mapped_inputs = inputs
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .and_then(|id| names.get(id))
                        .cloned()
                        .context("Unknown dependency")
                })
                .collect::<Result<Vec<_>>>()?;
            let object = expanded.as_object_mut().context("Invalid step")?;
            object.insert("step".to_owned(), json!(mapped));
            object.insert("input_from".to_owned(), json!(mapped_inputs));
            if entry.is_some() {
                object.insert("origin".to_owned(), json!({"pipeline_id":pipeline_id,"revision_id":revision_id,"block_id":id}));
            }
            if let Some(branches) =
                object.get_mut("branches").and_then(Value::as_object_mut)
            {
                for targets in branches.values_mut() {
                    let renamed = targets
                        .as_array()
                        .context("Invalid branch destinations")?
                        .iter()
                        .map(|value| {
                            value
                                .as_str()
                                .and_then(|id| names.get(id))
                                .cloned()
                                .context("Unknown branch destination")
                        })
                        .collect::<Result<Vec<_>>>()?;
                    *targets = json!(renamed);
                }
            }
            if step.get("type").and_then(Value::as_str) == Some("pipeline") {
                let reference: PipelineReference = serde_json::from_value(
                    object
                        .remove("pipeline_ref")
                        .context("Reference missing")?,
                )?;
                object.insert("type".to_owned(), json!("forward"));
                self.steps.push(expanded);
                let child = self
                    .definitions
                    .get(&(
                        reference.pipeline_id.clone(),
                        reference.revision_id.clone(),
                    ))
                    .context("Referenced pipeline revision unavailable")?;
                self.append(
                    &reference.pipeline_id,
                    &reference.revision_id,
                    child,
                    Some((&reference.input_source, &mapped)),
                )?;
            } else {
                self.steps.push(expanded);
            }
        }
        self.active.remove(pipeline_id);
        Ok(())
    }
}

/// Expands each occurrence while preserving pinned ontology and replay
/// identities.
pub fn compile(
    pipeline_id: &str,
    revision_id: &str,
    definition: &Value,
    definitions: &BTreeMap<(String, String), Value>,
) -> Result<Value> {
    Ok(
        compile_with_stats(pipeline_id, revision_id, definition, definitions)?
            .definition,
    )
}

/// Validated composition and budgets, including each referenced input
/// contract.
pub struct CompiledPipeline {
    /// Flat graph accepted by Vision without runtime dependency resolution.
    pub definition: Value,
    /// Authored sources and steps charged once per embedded occurrence.
    pub blocks: usize,
    /// Cross-pipeline transfers charged independently of local branch count.
    pub transfers: usize,
}

/// Shares exact admission counters with the editor without trusting client
/// totals.
#[tracing::instrument(skip_all, name = "pipeline.composition.compile")]
pub fn compile_with_stats(
    pipeline_id: &str,
    revision_id: &str,
    definition: &Value,
    definitions: &BTreeMap<(String, String), Value>,
) -> Result<CompiledPipeline> {
    let mut expansion = Expansion {
        definitions,
        active: BTreeSet::new(),
        steps: Vec::new(),
        blocks: 0,
        transfers: 0,
        next_id: 0,
    };
    expansion.append(pipeline_id, revision_id, definition, None)?;
    let mut result = definition.clone();
    result
        .as_object_mut()
        .context("Invalid pipeline")?
        .insert("pipeline".to_owned(), json!(expansion.steps));
    tracing::info!(
        blocks = expansion.blocks,
        transfers = expansion.transfers,
        "pipeline composition compiled"
    );
    Ok(CompiledPipeline {
        definition: result,
        blocks: expansion.blocks,
        transfers: expansion.transfers,
    })
}
