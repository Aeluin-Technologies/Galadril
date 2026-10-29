//! Atomic workspace mutations share Registry's optimistic tenant revision
//! boundary.

use anyhow::{Context, Result, ensure};
use chrono::Utc;

use crate::authoring::{
    AuthoringCommand, AuthoringState, Branch, ContentReview, MergeProposal,
    PipelineRelease, definition_hash, merge_definitions, valid_branch,
    validate_draft, validate_executable,
};
use crate::domain::{PipelineView, PutPipeline, Registry};
use crate::state::PipelineRecord;
use crate::storage::{RevisionStore, valid_tenant};

impl<S: RevisionStore> Registry<S> {
    /// Commits authoring state without allowing incomplete drafts into runtime
    /// discovery.
    #[tracing::instrument(skip_all, fields(operation = command.operation_name()))]
    pub async fn author_pipeline(
        &self,
        input: PutPipeline<'_>,
        command: AuthoringCommand,
    ) -> Result<PipelineView> {
        ensure!(
            valid_tenant(input.tenant_id) && valid_branch(input.pipeline_id),
            "invalid pipeline identity"
        );
        ensure!(
            !input.author.is_empty() && !input.owner_id.is_empty(),
            "pipeline principal required"
        );
        ensure!(
            !input.name.trim().is_empty() && input.name.len() <= 256,
            "pipeline name required"
        );
        ensure!(
            !input.message.is_empty() && input.message.len() <= 1024,
            "pipeline message required"
        );
        if input.create_only {
            ensure!(
                matches!(&command, AuthoringCommand::SaveDraft { branch } if branch == "main"),
                "only a main draft can create a pipeline"
            );
            self.store.ensure_tenant(input.tenant_id).await?;
        }
        let snapshot = self.store.load(input.tenant_id, None).await?;
        ensure!(
            input.create_only ||
                input.expected_revision_id ==
                    Some(snapshot.revision_id.as_str()),
            "pipeline head changed"
        );
        let mut state = snapshot.state;
        let previous = state.pipelines.remove(input.pipeline_id);
        ensure!(
            !input.create_only || previous.is_none(),
            "pipeline already exists"
        );
        ensure!(
            input.create_only ||
                previous
                    .as_ref()
                    .is_some_and(|record| record.deleted_at_ms.is_none()),
            "pipeline unavailable"
        );
        let now = Utc::now().timestamp_millis();
        let mut record = previous.unwrap_or_else(|| PipelineRecord {
            authoring: AuthoringState::default(),
            previous_revision_id: None,
            pipeline_id: input.pipeline_id.to_owned(),
            name: input.name.to_owned(),
            owner_id: input.owner_id.to_owned(),
            published_revision_id: None,
            definition: serde_json::Value::Null,
            author_id: input.author.to_owned(),
            message: input.message.to_owned(),
            created_at_ms: now,
            updated_at_ms: now,
            deleted_at_ms: None,
        });
        record.authoring.enabled = true;
        match command {
            AuthoringCommand::SaveDraft { branch } => {
                validate_draft(&input.definition)?;
                ensure!(
                    input
                        .definition
                        .get("name")
                        .and_then(serde_json::Value::as_str) ==
                        Some(input.name),
                    "pipeline name mismatch"
                );
                if branch == "main" {
                    record.definition = input.definition;
                    record.name = input.name.to_owned();
                    record.authoring.approval = None;
                } else {
                    let selected = record
                        .authoring
                        .branches
                        .get_mut(&branch)
                        .context("pipeline branch unavailable")?;
                    selected.definition = input.definition;
                    selected.author_id = input.author.to_owned();
                    selected.updated_at_ms = now;
                }
            },
            AuthoringCommand::CreateBranch {
                branch,
                from_branch,
            } => {
                ensure!(
                    valid_branch(&branch) && branch != "main",
                    "invalid pipeline branch"
                );
                ensure!(
                    record.authoring.branches.len() < 32 &&
                        !record.authoring.branches.contains_key(&branch),
                    "pipeline branch exists or limit reached"
                );
                let (definition, base_definition, base_revision_id) =
                    if from_branch == "main" {
                        (
                            &record.definition,
                            &record.definition,
                            &snapshot.revision_id,
                        )
                    } else {
                        let parent = record
                            .authoring
                            .branches
                            .get(&from_branch)
                            .context("pipeline branch unavailable")?;
                        (
                            &parent.definition,
                            &parent.base_definition,
                            &parent.base_revision_id,
                        )
                    };
                record.authoring.branches.insert(
                    branch,
                    Branch {
                        definition: definition.clone(),
                        base_definition: base_definition.clone(),
                        base_revision_id: base_revision_id.clone(),
                        author_id: input.author.to_owned(),
                        updated_at_ms: now,
                    },
                );
            },
            AuthoringCommand::ProposeMerge { source_branch } => {
                let source = record
                    .authoring
                    .branches
                    .get(&source_branch)
                    .context("pipeline source branch unavailable")?;
                ensure!(
                    record.authoring.proposals.len() < 32 ||
                        record
                            .authoring
                            .proposals
                            .contains_key(&source_branch),
                    "pipeline proposal limit exceeded"
                );
                record.authoring.proposals.insert(
                    source_branch.clone(),
                    MergeProposal {
                        source_branch,
                        source_hash: definition_hash(&source.definition)?,
                        target_hash: definition_hash(&record.definition)?,
                        author_id: input.author.to_owned(),
                        created_at_ms: now,
                    },
                );
            },
            AuthoringCommand::Merge {
                proposal_id,
                resolutions,
            } => {
                let proposal = record
                    .authoring
                    .proposals
                    .get(&proposal_id)
                    .context("pipeline proposal unavailable")?;
                let source = record
                    .authoring
                    .branches
                    .get(&proposal.source_branch)
                    .context("pipeline branch unavailable")?;
                ensure!(
                    proposal.source_hash ==
                        definition_hash(&source.definition)? &&
                        proposal.target_hash ==
                            definition_hash(&record.definition)?,
                    "pipeline proposal changed; refresh proposal"
                );
                let preview = merge_definitions(
                    &source.base_definition,
                    &record.definition,
                    &source.definition,
                    &resolutions,
                )?;
                ensure!(
                    preview.conflicts.is_empty(),
                    "pipeline merge conflicts unresolved"
                );
                validate_draft(&preview.definition)?;
                record.name = preview
                    .definition
                    .get("name")
                    .and_then(serde_json::Value::as_str)
                    .context("pipeline name missing")?
                    .to_owned();
                record.definition = preview.definition;
                record.authoring.approval = None;
                record.authoring.proposals.remove(&proposal_id);
                let source = record
                    .authoring
                    .branches
                    .get_mut(&proposal_id)
                    .context("pipeline branch unavailable")?;
                source.base_definition = record.definition.clone();
                source.definition = record.definition.clone();
                source.base_revision_id = snapshot.revision_id.clone();
            },
            AuthoringCommand::Approve => {
                validate_executable(&record.definition)?;
                ensure!(
                    !record
                        .definition
                        .get("sources")
                        .and_then(serde_json::Value::as_array)
                        .is_none_or(Vec::is_empty),
                    "pipeline requires a source"
                );
                self.compile_composition(
                    input.tenant_id,
                    input.pipeline_id,
                    &snapshot.revision_id,
                    &record.definition,
                    false,
                )
                .await?;
                record.authoring.approval = Some(ContentReview {
                    definition_hash: definition_hash(&record.definition)?,
                    author_id: input.author.to_owned(),
                    reviewed_at_ms: now,
                });
            },
            AuthoringCommand::Publish => {
                let review = record
                    .authoring
                    .approval
                    .as_ref()
                    .context("pipeline approval required")?;
                ensure!(
                    review.definition_hash ==
                        definition_hash(&record.definition)?,
                    "pipeline approval stale"
                );
                self.compile_composition(
                    input.tenant_id,
                    input.pipeline_id,
                    &snapshot.revision_id,
                    &record.definition,
                    true,
                )
                .await?;
                record.authoring.release = Some(PipelineRelease {
                    revision_id: snapshot.revision_id.clone(),
                    definition_hash: review.definition_hash.clone(),
                    author_id: input.author.to_owned(),
                    published_at_ms: now,
                });
            },
            AuthoringCommand::Deploy => {
                let release = record
                    .authoring
                    .release
                    .as_ref()
                    .context("pipeline publication required")?;
                let pinned = self
                    .pipeline_revision(
                        input.tenant_id,
                        input.pipeline_id,
                        &release.revision_id,
                    )
                    .await?;
                ensure!(
                    release.definition_hash ==
                        definition_hash(&pinned.record.definition)?,
                    "pipeline publication invalid"
                );
                self.compile_composition(
                    input.tenant_id,
                    input.pipeline_id,
                    &release.revision_id,
                    &pinned.record.definition,
                    true,
                )
                .await?;
                self.runtime_pipeline(
                    input.tenant_id,
                    input.pipeline_id,
                    &release.revision_id,
                )
                .await?;
                record.published_revision_id =
                    Some(release.revision_id.clone());
            },
        }
        record.previous_revision_id =
            (!input.create_only).then_some(snapshot.revision_id.clone());
        record.author_id = input.author.to_owned();
        record.message = input.message.to_owned();
        record.updated_at_ms = now;
        ensure!(
            serde_json::to_vec(&record)?.len() <= 3 * 1024 * 1024,
            "pipeline workspace size limit exceeded"
        );
        state.pipelines.insert(input.pipeline_id.to_owned(), record);
        let head = self
            .store
            .commit(
                input.tenant_id,
                &snapshot.revision_id,
                &state,
                input.author,
                input.message,
            )
            .await?;
        let record = state
            .pipelines
            .remove(input.pipeline_id)
            .context("committed pipeline missing")?;
        Ok(PipelineView {
            head_revision_id: head,
            record,
        })
    }
}

impl AuthoringCommand {
    /// Supplies bounded telemetry labels without user content or branch
    /// identities.
    pub fn operation_name(&self) -> &'static str {
        match self {
            Self::SaveDraft { .. } => "save_draft",
            Self::CreateBranch { .. } => "create_branch",
            Self::ProposeMerge { .. } => "propose_merge",
            Self::Merge { .. } => "merge",
            Self::Approve => "approve",
            Self::Publish => "publish",
            Self::Deploy => "deploy",
        }
    }
}
