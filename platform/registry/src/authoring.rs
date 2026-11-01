//! Bounded authoring state and deterministic, block-aware three-way merges.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// Commands are explicit so draft writes cannot impersonate lifecycle
/// transitions.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum AuthoringCommand {
    /// Saves incomplete authored content on a selected branch.
    SaveDraft {
        /// Selected branch, including the reserved main branch.
        branch: String,
    },
    /// Atomically enables a block contract and records its published binding.
    BindOntology {
        /// Executable block in the current main draft.
        block_id: String,
        /// Tenant-local production ontology.
        ontology_id: String,
    },
    /// Forks a branch from its current definition and immutable provenance.
    CreateBranch {
        /// New tenant-local branch name.
        branch: String,
        /// Existing branch to fork.
        from_branch: String,
    },
    /// Pins a source branch and main for a reviewed merge proposal.
    ProposeMerge {
        /// Branch proposed for merging into main.
        source_branch: String,
    },
    /// Applies a proposal after recomputing conflicts against the pinned
    /// content.
    Merge {
        /// Proposal identity, equal to its source branch name.
        proposal_id: String,
        /// Explicit choices for every conflicting JSON pointer.
        resolutions: BTreeMap<String, MergeChoice>,
    },
    /// Approves the current, executable main definition.
    Approve,
    /// Pins approved content without changing runtime discovery.
    Publish,
    /// Activates the published pin through the existing runtime pointer.
    Deploy,
}

/// A conflict choice never accepts arbitrary executable content.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MergeChoice {
    /// Retains main's value, including a deletion.
    Target,
    /// Accepts the proposed source value, including a deletion.
    Source,
}

/// Collaboration metadata is persisted with the same atomic tenant commit.
#[derive(Debug, Default, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AuthoringState {
    /// Managed workspaces require explicit review and deployment commands.
    pub enabled: bool,
    /// Additional branches; main remains the canonical pipeline definition.
    pub branches: BTreeMap<String, Branch>,
    /// Pending reviewed proposals keyed by source branch.
    pub proposals: BTreeMap<String, MergeProposal>,
    /// Approval bound to the complete main definition.
    pub approval: Option<ContentReview>,
    /// Published immutable definition awaiting or selected for deployment.
    pub release: Option<PipelineRelease>,
}

/// A fork carries its base content to support deterministic three-way merging.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Branch {
    /// Recorded fork origin; older drafts have no reliable ancestry metadata.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_branch: Option<String>,
    /// Authored branch document, including editor layout.
    pub definition: Value,
    /// Unmodified common ancestor selected when the branch was forked.
    pub base_definition: Value,
    /// Immutable tenant revision at the fork point.
    pub base_revision_id: String,
    /// Authenticated last editor.
    pub author_id: String,
    /// Last edit time in Unix milliseconds.
    pub updated_at_ms: i64,
}

/// A proposal remains reviewable until either participating branch changes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MergeProposal {
    /// Source branch whose content is pinned by this proposal.
    pub source_branch: String,
    /// Content hash of the source at proposal creation.
    pub source_hash: String,
    /// Content hash of main at proposal creation.
    pub target_hash: String,
    /// Authenticated proposing principal.
    pub author_id: String,
    /// Creation time in Unix milliseconds.
    pub created_at_ms: i64,
}

/// Review provenance cannot be supplied inside an authored definition.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContentReview {
    /// Hash of exactly the content that was validated and reviewed.
    pub definition_hash: String,
    /// Authenticated reviewing principal.
    pub author_id: String,
    /// Review time in Unix milliseconds.
    pub reviewed_at_ms: i64,
}

/// Publication is independent of the runtime activation pointer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PipelineRelease {
    /// Existing immutable revision containing the approved main definition.
    pub revision_id: String,
    /// Reviewed content hash used to reject stale release transitions.
    pub definition_hash: String,
    /// Authenticated publishing principal.
    pub author_id: String,
    /// Publication time in Unix milliseconds.
    pub published_at_ms: i64,
}

/// Stable path-level conflict information for an explicit review UI.
#[derive(Debug, Serialize, Deserialize)]
pub struct MergeConflict {
    /// JSON pointer with source and pipeline arrays keyed by block identity.
    pub path: String,
    /// Common ancestor value; absent values indicate deletion.
    pub base: Option<Value>,
    /// Main branch value.
    pub target: Option<Value>,
    /// Proposed branch value.
    pub source: Option<Value>,
}

/// A preview never persists content or silently resolves a conflict.
#[derive(Debug, Serialize, Deserialize)]
pub struct MergePreview {
    /// Candidate definition with main retained for unresolved conflicts.
    pub definition: Value,
    /// Conflicts still requiring an explicit choice.
    pub conflicts: Vec<MergeConflict>,
}

/// Hashes canonical JSON so approvals cannot survive content changes.
pub fn definition_hash(definition: &Value) -> Result<String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(definition)?)
    ))
}

/// Rejects service settings while accepting incomplete graph configuration.
pub fn validate_draft(definition: &Value) -> Result<()> {
    let object = definition
        .as_object()
        .context("pipeline draft not object")?;
    ensure!(
        object.keys().all(|key| matches!(
            key.as_str(),
            "name" | "version" | "sources" | "pipeline" | "editor"
        )),
        "pipeline service settings forbidden"
    );
    ensure!(
        object
            .get("name")
            .and_then(Value::as_str)
            .is_some_and(|name| !name.trim().is_empty() && name.len() <= 256),
        "pipeline name required"
    );
    let sources = object
        .get("sources")
        .and_then(Value::as_array)
        .context("pipeline sources missing")?;
    let steps = object
        .get("pipeline")
        .and_then(Value::as_array)
        .context("pipeline steps missing")?;
    ensure!(
        sources.len() + steps.len() <= 256,
        "pipeline block limit exceeded"
    );
    ensure!(
        serde_json::to_vec(definition)?.len() <= 1_048_576,
        "pipeline document limit exceeded"
    );
    let mut ids = BTreeSet::new();
    for (nodes, key) in [(sources, "id"), (steps, "step")] {
        for node in nodes {
            let id = node
                .get(key)
                .and_then(Value::as_str)
                .context("pipeline block identity missing")?;
            ensure!(
                valid_branch(id) &&
                    !id.starts_with("__linked_") &&
                    ids.insert(id),
                "pipeline block identity invalid or duplicate"
            );
            ensure!(
                node.get("origin").is_none(),
                "reserved pipeline compiler field"
            );
        }
    }
    Ok(())
}

/// Uses bounded path-safe names for branches and block identities.
pub fn valid_branch(branch: &str) -> bool {
    !branch.is_empty() &&
        branch.len() <= 128 &&
        branch.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
        })
}

fn normalize(definition: &Value) -> Result<Value> {
    let mut value = definition.clone();
    let object = value
        .as_object_mut()
        .context("pipeline definition not object")?;
    for (collection, key) in [("sources", "id"), ("pipeline", "step")] {
        let nodes = object
            .get(collection)
            .and_then(Value::as_array)
            .context("pipeline collection missing")?;
        let mut indexed = Map::new();
        for node in nodes {
            let id = node
                .get(key)
                .and_then(Value::as_str)
                .context("pipeline block identity missing")?;
            ensure!(
                indexed.insert(id.to_owned(), node.clone()).is_none(),
                "duplicate pipeline block"
            );
        }
        object.insert(collection.to_owned(), Value::Object(indexed));
    }
    Ok(value)
}

fn pointer(path: &str, key: &str) -> String {
    format!("{path}/{}", key.replace('~', "~0").replace('/', "~1"))
}

fn merge_value(
    path: &str,
    base: Option<&Value>,
    target: Option<&Value>,
    source: Option<&Value>,
    resolutions: &BTreeMap<String, MergeChoice>,
    conflicts: &mut Vec<MergeConflict>,
    depth: usize,
) -> Result<Option<Value>> {
    ensure!(depth <= 32, "pipeline merge depth exceeded");
    if target == source || source == base {
        return Ok(target.cloned());
    }
    if target == base {
        return Ok(source.cloned());
    }
    if let (Some(Value::Object(left)), Some(Value::Object(right))) =
        (target, source) &&
        base.is_none_or(Value::is_object)
    {
        let ancestor = base.and_then(Value::as_object);
        let keys: BTreeSet<_> = left
            .keys()
            .chain(right.keys())
            .chain(ancestor.into_iter().flat_map(Map::keys))
            .collect();
        let mut result = Map::new();
        for key in keys {
            if let Some(value) = merge_value(
                &pointer(path, key),
                ancestor.and_then(|item| item.get(key)),
                left.get(key),
                right.get(key),
                resolutions,
                conflicts,
                depth + 1,
            )? {
                result.insert(key.clone(), value);
            }
        }
        return Ok(Some(Value::Object(result)));
    }
    if let Some(choice) = resolutions.get(path) {
        return Ok(match choice {
            MergeChoice::Target => target.cloned(),
            MergeChoice::Source => source.cloned(),
        });
    }
    conflicts.push(MergeConflict {
        path: path.to_owned(),
        base: base.cloned(),
        target: target.cloned(),
        source: source.cloned(),
    });
    Ok(target.cloned())
}

/// Merges blocks by stable identity instead of treating an entire graph as one
/// array.
pub fn merge_definitions(
    base: &Value,
    target: &Value,
    source: &Value,
    resolutions: &BTreeMap<String, MergeChoice>,
) -> Result<MergePreview> {
    let base = normalize(base)?;
    let target = normalize(target)?;
    let source = normalize(source)?;
    ensure!(
        resolutions.len() <= 256,
        "pipeline resolution limit exceeded"
    );
    let mut conflicts = Vec::new();
    let mut definition = merge_value(
        "",
        Some(&base),
        Some(&target),
        Some(&source),
        resolutions,
        &mut conflicts,
        0,
    )?
    .context("pipeline merge removed definition")?;
    let object = definition
        .as_object_mut()
        .context("pipeline merge not object")?;
    for key in ["sources", "pipeline"] {
        let indexed = object
            .get(key)
            .and_then(Value::as_object)
            .context("pipeline merge collection missing")?;
        object.insert(
            key.to_owned(),
            Value::Array(indexed.values().cloned().collect()),
        );
    }
    Ok(MergePreview {
        definition,
        conflicts,
    })
}

/// Validates configuration fields that structural graph checks alone cannot
/// guarantee.
pub fn validate_executable(definition: &Value) -> Result<()> {
    validate_draft(definition)?;
    crate::semantics::validate_pipeline_structure(definition)?;
    let sources = definition
        .get("sources")
        .and_then(Value::as_array)
        .context("pipeline sources missing")?;
    ensure!(!sources.is_empty(), "pipeline source required");
    for source in sources {
        if source.get("capability_id").and_then(Value::as_str) ==
            Some("utils.input.email")
        {
            ensure!(
                source
                    .get("account_id")
                    .and_then(Value::as_str)
                    .is_some_and(valid_mailbox),
                "email receiving account required"
            );
        }
        for field in ["topic", "match_pattern", "schema_path"] {
            ensure!(
                source
                    .get(field)
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.trim().is_empty()),
                "pipeline source field missing: {field}"
            );
        }
    }
    let steps = definition
        .get("pipeline")
        .and_then(Value::as_array)
        .context("pipeline steps missing")?;
    for step in steps {
        if step.get("capability_id").and_then(Value::as_str) ==
            Some("utils.output.reply")
        {
            ensure!(
                step.pointer("/params/sender_account_id")
                    .and_then(Value::as_str)
                    .is_some_and(
                        |value| value == "source" || valid_mailbox(value)
                    ),
                "email sending account required"
            );
            ensure!(
                matches!(
                    step.pointer("/params/delivery_mode")
                        .and_then(Value::as_str),
                    Some("automatic" | "approval" | "draft")
                ),
                "email delivery mode required"
            );
        }
        let kind = step
            .get("type")
            .and_then(Value::as_str)
            .context("pipeline type missing")?;
        let inputs = step
            .get("input_from")
            .and_then(Value::as_array)
            .context("pipeline input missing")?;
        let detached = matches!(
            step.pointer("/params/trigger").and_then(Value::as_str),
            Some("cron" | "on_demand")
        );
        let union = kind == "forward" &&
            step.pointer("/params/merge").and_then(Value::as_str) ==
                Some("union");
        ensure!(
            detached || (!inputs.is_empty() && (inputs.len() == 1 || union)),
            "pipeline input cardinality invalid"
        );
        if kind == "filter" ||
            step.get("routing").and_then(Value::as_str) ==
                Some("conditions")
        {
            let conditions = step
                .pointer("/params/conditions")
                .and_then(Value::as_array)
                .context("pipeline conditions missing")?;
            ensure!(
                !conditions.is_empty() && conditions.len() <= 16,
                "pipeline condition count invalid"
            );
            for condition in conditions {
                ensure!(
                    matches!(
                        condition.get("field").and_then(Value::as_str),
                        Some(
                            "sender" |
                                "recipient" |
                                "subject" |
                                "body" |
                                "account"
                        )
                    ),
                    "pipeline condition field invalid"
                );
                ensure!(
                    matches!(
                        condition.get("operator").and_then(Value::as_str),
                        Some(
                            "equals" |
                                "contains" |
                                "starts_with" |
                                "not_equals"
                        )
                    ),
                    "pipeline condition operator invalid"
                );
                ensure!(
                    condition
                        .get("value")
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.trim().is_empty() &&
                            value.len() <= 2048),
                    "pipeline condition value invalid"
                );
            }
        }
    }
    Ok(())
}

fn valid_mailbox(value: &str) -> bool {
    value.len() == 32 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

/// One semantic edit, addressed by stable block identity rather than array
/// index.
#[derive(Debug, Serialize, Deserialize)]
pub struct DefinitionChange {
    /// JSON pointer into the identity-keyed authoring document.
    pub path: String,
    /// Previous value; absent indicates a newly added field or block.
    pub before: Option<Value>,
    /// New value; absent indicates removal.
    pub after: Option<Value>,
}

fn collect_changes(
    path: &str,
    before: Option<&Value>,
    after: Option<&Value>,
    result: &mut Vec<DefinitionChange>,
    depth: usize,
) -> Result<()> {
    ensure!(depth <= 32, "pipeline comparison depth exceeded");
    if before == after {
        return Ok(());
    }
    if let (Some(Value::Object(left)), Some(Value::Object(right))) =
        (before, after)
    {
        for key in left.keys().chain(right.keys()).collect::<BTreeSet<_>>() {
            collect_changes(
                &pointer(path, key),
                left.get(key),
                right.get(key),
                result,
                depth + 1,
            )?;
        }
    } else {
        result.push(DefinitionChange {
            path: path.to_owned(),
            before: before.cloned(),
            after: after.cloned(),
        });
    }
    Ok(())
}

/// Compares immutable authored revisions without expanding linked
/// dependencies.
pub fn compare_definitions(
    before: &Value,
    after: &Value,
) -> Result<Vec<DefinitionChange>> {
    let before = normalize(before)?;
    let after = normalize(after)?;
    let mut changes = Vec::new();
    collect_changes("", Some(&before), Some(&after), &mut changes, 0)?;
    Ok(changes)
}

/// Bounded public diagnostic codes never contain provider failures or
/// credentials.
#[derive(Debug, Serialize, Deserialize)]
pub struct PipelineDiagnostic {
    /// Stable user-facing localization key.
    pub code: String,
    /// Authored block requiring attention, if the issue is local.
    pub node_id: Option<String>,
    /// Configuration field requiring attention.
    pub field: Option<String>,
}

/// Collects independent configuration issues before attempting authoritative
/// composition.
pub fn diagnose_definition(
    definition: &Value,
) -> Result<Vec<PipelineDiagnostic>> {
    validate_draft(definition)?;
    let sources = definition
        .get("sources")
        .and_then(Value::as_array)
        .context("pipeline sources missing")?;
    let steps = definition
        .get("pipeline")
        .and_then(Value::as_array)
        .context("pipeline steps missing")?;
    let mut issues = Vec::new();
    let mut add = |code: &str, node: Option<&str>, field: Option<&str>| {
        issues.push(PipelineDiagnostic {
            code: code.to_owned(),
            node_id: node.map(str::to_owned),
            field: field.map(str::to_owned),
        })
    };
    if sources.is_empty() {
        add("source", None, None);
    }
    for source in sources {
        for field in ["topic", "match_pattern", "schema_path"] {
            if !source
                .get(field)
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty())
            {
                add(
                    "field",
                    source.get("id").and_then(Value::as_str),
                    Some(field),
                );
            }
        }
    }
    let known: BTreeSet<_> = sources
        .iter()
        .filter_map(|source| source.get("id").and_then(Value::as_str))
        .chain(
            steps
                .iter()
                .filter_map(|step| step.get("step").and_then(Value::as_str)),
        )
        .collect();
    for step in steps {
        let id = step.get("step").and_then(Value::as_str);
        let kind =
            step.get("type").and_then(Value::as_str).unwrap_or_default();
        let inputs = step.get("input_from").and_then(Value::as_array);
        if !matches!(
            step.pointer("/params/trigger").and_then(Value::as_str),
            Some("cron" | "on_demand")
        ) && inputs.is_none_or(Vec::is_empty)
        {
            add("input", id, Some("input_from"));
        }
        if inputs.is_some_and(|items| {
            items.iter().any(|input| {
                input.as_str().is_none_or(|input| !known.contains(input))
            })
        }) {
            add("connection", id, Some("input_from"));
        }
        if inputs.is_some_and(|items| items.len() > 1) &&
            !(kind == "forward" &&
                step.pointer("/params/merge").and_then(Value::as_str) ==
                    Some("union"))
        {
            add("join", id, Some("input_from"));
        }
        if kind == "inference" &&
            step.get("model").is_none_or(Value::is_null) &&
            step.get("capability_id").is_none_or(Value::is_null)
        {
            add("model", id, Some("model"));
        }
        if kind == "pipeline" &&
            step.get("pipeline_ref").is_none_or(Value::is_null)
        {
            add("reference", id, Some("pipeline_ref"));
        }
        let uses_conditions = kind == "filter" ||
            step.get("routing").and_then(Value::as_str) ==
                Some("conditions");
        if uses_conditions &&
            !step
                .pointer("/params/conditions")
                .and_then(Value::as_array)
                .is_some_and(|rules| {
                    !rules.is_empty() &&
                        rules.iter().all(|rule| {
                            rule.get("value")
                                .and_then(Value::as_str)
                                .is_some_and(|value| !value.trim().is_empty())
                        })
                })
        {
            add("field", id, Some("conditions"));
        }
    }
    Ok(issues)
}
