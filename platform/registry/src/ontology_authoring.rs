//! Ontology collaboration changes share the immutable tenant commit boundary.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, ensure};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::authoring::valid_branch;
use crate::domain::{OntologyView, PutOntology, Registry, validate_identity};
use crate::semantics::{merge_ontology, validate_ontology};
use crate::state::{OntologyRecord, TenantState};
use crate::storage::{RevisionStore, valid_revision};

/// Explicit operations never publish a branch or rewrite an existing revision.
#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum OntologyCommand {
    /// Forks one current workspace definition.
    CreateBranch {
        /// New tenant-local branch name.
        branch: String,
        /// Existing parent branch, including main.
        from_branch: String,
    },
    /// Replaces one branch with a separately validated ontology document.
    SaveDraft {
        /// Branch receiving the supplied document.
        branch: String,
    },
    /// Merges into the parent after recomputing semantic conflicts.
    Merge {
        /// Source branch; main cannot be merged into itself.
        branch: String,
    },
    /// Removes a leaf branch without removing immutable history.
    DeleteBranch {
        /// Existing non-main branch.
        branch: String,
    },
    /// Hides an unused native ontology while retaining its immutable evidence.
    Delete,
    /// Restores the draft without reactivating a production publication.
    Restore,
}

/// Branch definitions remain control-plane data, outside semantic model
/// hashes.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OntologyBranch {
    /// Branch receiving a future merge.
    pub parent_branch: String,
    /// Current authored model.
    pub definition: Value,
    /// Name authored alongside the model.
    pub display_name: String,
    /// Common ancestor used by semantic three-way merge.
    pub base_definition: Value,
    /// Name at the common ancestor.
    pub base_display_name: String,
    /// Immutable snapshot containing the forked or last merged source state.
    pub base_revision_id: String,
    /// Authenticated last editor.
    pub author: String,
    /// Last edit time in Unix milliseconds.
    pub updated_at_ms: i64,
}

/// Additive workspace state can be reconstructed from older ontology records.
#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct OntologyAuthoringState {
    /// Independent experimental definitions keyed by branch name.
    pub branches: BTreeMap<String, OntologyBranch>,
    /// Snapshot before the last mutation of this ontology.
    pub previous_revision_id: Option<String>,
    /// Branch affected by the last operation.
    pub last_branch: String,
    /// Bounded operation name for history and telemetry.
    pub last_operation: String,
    /// Authenticated editor, or Registry for legacy lifecycle operations.
    pub last_author: String,
    /// Last ontology mutation time in Unix milliseconds.
    pub updated_at_ms: i64,
    /// Tombstone time; historical revisions remain available.
    pub deleted_at_ms: Option<i64>,
}

impl OntologyAuthoringState {
    /// Records metadata independently of the model's semantic provenance.
    pub(crate) fn touch(
        &mut self,
        previous: Option<String>,
        branch: &str,
        operation: &str,
        author: &str,
    ) {
        self.previous_revision_id = previous;
        self.last_branch = branch.to_owned();
        self.last_operation = operation.to_owned();
        self.last_author = author.to_owned();
        self.updated_at_ms = Utc::now().timestamp_millis();
    }
}

/// One revision summary contains no storage coordinates or unrelated models.
#[derive(Debug, Serialize)]
pub struct OntologyHistoryItem {
    /// Immutable tenant snapshot containing this model state.
    pub revision_id: String,
    /// Verified editor recorded for the operation.
    pub author: String,
    /// Recorded revision explanation.
    pub message: String,
    /// Branch affected by the operation.
    pub branch: String,
    /// Bounded operation name.
    pub operation: String,
    /// Operation time in Unix milliseconds.
    pub created_at_ms: i64,
}

/// Pagination follows only this ontology's immutable revision chain.
#[derive(Debug, Serialize)]
pub struct OntologyHistoryPage {
    /// Newest-first metadata for the requested ontology.
    pub items: Vec<OntologyHistoryItem>,
    /// Next snapshot to inspect, when older entries remain.
    pub next_revision_id: Option<String>,
}

impl OntologyCommand {
    /// Supplies bounded labels without authored content or branch identities.
    pub fn operation_name(&self) -> &'static str {
        match self {
            Self::CreateBranch { .. } => "create_branch",
            Self::SaveDraft { .. } => "save_draft",
            Self::Merge { .. } => "merge",
            Self::DeleteBranch { .. } => "delete_branch",
            Self::Delete => "delete",
            Self::Restore => "restore",
        }
    }
}

impl OntologyRecord {
    /// Clones one explicit ancestor so a later merge can detect conflicting
    /// changes.
    fn fork(
        &mut self,
        branch: &str,
        from_branch: &str,
        expected: &str,
        author: &str,
        now: i64,
    ) -> Result<()> {
        ensure!(
            valid_branch(branch) &&
                branch != "main" &&
                !self.authoring.branches.contains_key(branch),
            "invalid ontology branch"
        );
        ensure!(
            self.authoring.branches.len() < 20,
            "ontology branch limit exceeded"
        );
        let (definition, name) = if from_branch == "main" {
            (&self.ontology, &self.display_name)
        } else {
            let parent = self
                .authoring
                .branches
                .get(from_branch)
                .context("parent ontology branch unavailable")?;
            (&parent.definition, &parent.display_name)
        };
        let value = OntologyBranch {
            parent_branch: from_branch.to_owned(),
            definition: definition.clone(),
            display_name: name.clone(),
            base_definition: definition.clone(),
            base_display_name: name.clone(),
            base_revision_id: expected.to_owned(),
            author: author.to_owned(),
            updated_at_ms: now,
        };
        self.authoring.branches.insert(branch.to_owned(), value);

        Ok(())
    }

    /// Uses the transport budget for both initial and subsequent workspace
    /// writes.
    pub(crate) fn validate_workspace_size(&self) -> Result<()> {
        ensure!(
            serde_json::to_vec(self)?.len() <= 2 * 1024 * 1024,
            "ontology workspace size limit exceeded"
        );
        Ok(())
    }
}

fn save_branch(
    record: &mut OntologyRecord,
    branch: &str,
    input: PutOntology<'_>,
    now: i64,
) -> Result<()> {
    ensure!(valid_branch(branch), "invalid ontology branch");
    ensure!(
        !input.display_name.trim().is_empty() &&
            input.display_name.len() <= 256,
        "invalid ontology display name"
    );
    validate_ontology(&input.ontology)?;
    ensure!(
        record.ontology.get("plugin_origin") ==
            input.ontology.get("plugin_origin"),
        "ontology plugin provenance is immutable"
    );
    if branch == "main" {
        record.ontology = input.ontology;
        record.display_name = input.display_name.to_owned();
    } else {
        let selected = record
            .authoring
            .branches
            .get_mut(branch)
            .context("ontology branch unavailable")?;
        selected.definition = input.ontology;
        selected.display_name = input.display_name.to_owned();
        selected.author = input.author.to_owned();
        selected.updated_at_ms = now;
    }
    Ok(())
}

fn merge_branch(
    record: &mut OntologyRecord,
    branch: &str,
    expected: &str,
    author: &str,
    now: i64,
) -> Result<String> {
    ensure!(branch != "main", "invalid ontology merge source");
    let source = record
        .authoring
        .branches
        .get(branch)
        .context("ontology branch unavailable")?;
    let parent = source.parent_branch.clone();
    let (target, name) = if parent == "main" {
        (&record.ontology, &record.display_name)
    } else {
        let value = record
            .authoring
            .branches
            .get(&parent)
            .context("parent ontology branch unavailable")?;
        (&value.definition, &value.display_name)
    };
    ensure!(
        name == &source.base_display_name ||
            source.display_name == source.base_display_name ||
            name == &source.display_name,
        "ontology name conflict"
    );
    let result =
        merge_ontology(&source.base_definition, target, &source.definition)?;
    ensure!(result.conflicts.is_empty(), "ontology merge has conflicts");
    let merged = result.ontology.context("ontology merge unavailable")?;
    let merged_name = if name == &source.base_display_name {
        source.display_name.clone()
    } else {
        name.clone()
    };
    let source = record
        .authoring
        .branches
        .get_mut(branch)
        .context("ontology branch unavailable")?;
    source.base_definition = source.definition.clone();
    source.base_display_name = source.display_name.clone();
    source.base_revision_id = expected.to_owned();
    if parent == "main" {
        record.ontology = merged;
        record.display_name = merged_name;
    } else {
        let value = record
            .authoring
            .branches
            .get_mut(&parent)
            .context("parent ontology branch unavailable")?;
        value.definition = merged;
        value.display_name = merged_name;
        value.author = author.to_owned();
        value.updated_at_ms = now;
    }
    Ok(parent)
}

fn remove_branch(record: &mut OntologyRecord, branch: &str) -> Result<()> {
    ensure!(
        branch != "main" && record.authoring.branches.contains_key(branch),
        "ontology branch unavailable"
    );
    ensure!(
        !record
            .authoring
            .branches
            .values()
            .any(|child| child.parent_branch == branch),
        "ontology branch has children"
    );
    record.authoring.branches.remove(branch);
    Ok(())
}

fn delete_ontology(
    record: &mut OntologyRecord,
    state: &TenantState,
    now: i64,
) -> Result<()> {
    ensure!(
        record.ontology.get("plugin_origin").is_none(),
        "plugin ontology is protected"
    );
    ensure!(
        !state
            .bindings
            .values()
            .any(|binding| binding.ontology_id == record.ontology_id.as_str()),
        "ontology is used by a pipeline"
    );
    ensure!(
        !state.ontologies.values().any(|value| value
            .authoring
            .deleted_at_ms
            .is_none() &&
            value
                .ontology
                .pointer("/target_origin/ontology_id")
                .and_then(Value::as_str) ==
                Some(record.ontology_id.as_str())),
        "ontology is used by another model"
    );
    record.authoring.deleted_at_ms = Some(now);
    record.production_publication = None;
    Ok(())
}

impl<S: RevisionStore> Registry<S> {
    /// Commits one authorized workspace command using the expected tenant
    /// head.
    pub async fn author_ontology(
        &self,
        input: PutOntology<'_>,
        command: OntologyCommand,
    ) -> Result<OntologyView> {
        validate_identity(input.tenant_id, input.ontology_id)?;
        ensure!(
            !input.create_only,
            "ontology commands require an existing model"
        );
        ensure!(
            !input.author.is_empty() && input.author.len() <= 256,
            "invalid ontology author"
        );
        let expected = input
            .expected_revision_id
            .context("expected revision ID is required")?;
        ensure!(valid_revision(expected), "invalid ontology revision");
        let snapshot = self.store.load(input.tenant_id, None).await?;
        ensure!(snapshot.revision_id == expected, "ontology head changed");
        let mut state = snapshot.state;
        let mut record = state
            .ontologies
            .remove(input.ontology_id)
            .context("ontology unavailable")?;
        ensure!(
            record.authoring.deleted_at_ms.is_none() ||
                matches!(&command, OntologyCommand::Restore),
            "ontology is deleted"
        );
        let operation = command.operation_name();
        let now = Utc::now().timestamp_millis();
        let (tenant, id, author) =
            (input.tenant_id, input.ontology_id, input.author);
        let affected = match command {
            OntologyCommand::CreateBranch {
                branch,
                from_branch,
            } => {
                record.fork(&branch, &from_branch, expected, author, now)?;
                branch
            },
            OntologyCommand::SaveDraft { branch } => {
                save_branch(&mut record, &branch, input, now)?;
                branch
            },
            OntologyCommand::Merge { branch } => {
                merge_branch(&mut record, &branch, expected, author, now)?
            },
            OntologyCommand::DeleteBranch { branch } => {
                remove_branch(&mut record, &branch)?;
                branch
            },
            OntologyCommand::Delete => {
                delete_ontology(&mut record, &state, now)?;
                "main".to_owned()
            },
            OntologyCommand::Restore => {
                ensure!(
                    record.authoring.deleted_at_ms.is_some(),
                    "ontology is not deleted"
                );
                record.authoring.deleted_at_ms = None;
                "main".to_owned()
            },
        };
        record.authoring.touch(
            Some(snapshot.revision_id.clone()),
            &affected,
            operation,
            author,
        );
        record.author = author.to_owned();
        record.message = format!("Ontology {operation}");
        record.validate_workspace_size()?;
        state.ontologies.insert(id.to_owned(), record);
        let revision_id = self
            .store
            .commit(tenant, expected, &state, author, operation)
            .await?;
        let record = state
            .ontologies
            .remove(id)
            .context("committed ontology unavailable")?;
        Ok(OntologyView {
            revision_id,
            record,
        })
    }

    /// Reads bounded history without walking unrelated tenant artifacts.
    pub async fn ontology_history(
        &self,
        tenant: &str,
        id: &str,
        after: Option<&str>,
        limit: usize,
    ) -> Result<OntologyHistoryPage> {
        validate_identity(tenant, id)?;
        ensure!((1..=100).contains(&limit), "invalid ontology history limit");
        if let Some(revision) = after {
            ensure!(valid_revision(revision), "invalid ontology revision");
        }
        let mut next = after.map(str::to_owned);
        let mut items = Vec::with_capacity(limit);
        let mut visited = BTreeSet::new();
        for _ in 0..limit {
            let mut snapshot =
                self.store.load(tenant, next.as_deref()).await?;
            ensure!(
                visited.insert(snapshot.revision_id.clone()),
                "ontology revision cycle"
            );
            let record = snapshot
                .state
                .ontologies
                .remove(id)
                .context("ontology unavailable at revision")?;
            let metadata = record.authoring;
            items.push(OntologyHistoryItem {
                revision_id: snapshot.revision_id,
                author: if metadata.last_author.is_empty() {
                    record.author
                } else {
                    metadata.last_author
                },
                message: record.message,
                branch: if metadata.last_branch.is_empty() {
                    "main".into()
                } else {
                    metadata.last_branch
                },
                operation: if metadata.last_operation.is_empty() {
                    "save_draft".into()
                } else {
                    metadata.last_operation
                },
                created_at_ms: if metadata.updated_at_ms == 0 {
                    record.created_at_ms
                } else {
                    metadata.updated_at_ms
                },
            });
            next = metadata.previous_revision_id;
            if next.is_none() {
                break;
            }
        }
        Ok(OntologyHistoryPage {
            items,
            next_revision_id: next,
        })
    }
}
