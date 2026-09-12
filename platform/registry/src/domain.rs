//! Registry domain service over immutable tenant revisions.

use anyhow::{Context, Result, ensure};
use chrono::Utc;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::semantics::{
    MergeResult, PipelineBinding, SemanticChange, merge_ontology,
    semantic_diff, slice_ontology, validate_ontology,
    validate_ontology_selector, validate_pipeline,
    validate_pipeline_structure,
};
use crate::state::{
    OntologyBindingRecord, OntologyPublicationRecord, OntologyRecord,
    PipelineRecord, TenantState,
};
use crate::storage::{RevisionStore, Snapshot, valid_revision, valid_tenant};

const MAX_TENANT_VALIDATIONS: usize = 100;

#[derive(Debug)]
/// Tenant projection whose revision is resolved entirely by Registry.
pub struct TenantView {
    /// Validated tenant identity.
    pub tenant_id: String,
    /// Immutable current lakeFS head.
    pub head_revision_id: String,
}

#[derive(Debug)]
/// Pipeline record paired with the lakeFS revision that produced the view.
pub struct PipelineView {
    /// Immutable revision or current branch head used for the view.
    pub head_revision_id: String,
    /// Validated pipeline projection.
    pub record: PipelineRecord,
}

/// Input for optimistic pipeline creation or replacement.
pub struct PutPipeline<'a> {
    /// Gateway-authorized tenant identity.
    pub tenant_id: &'a str,
    /// Stable tenant-local pipeline identity.
    pub pipeline_id: &'a str,
    /// Human-readable pipeline name.
    pub name: &'a str,
    /// Gateway-authorized owner identity.
    pub owner_id: &'a str,
    /// Candidate pipeline configuration document.
    pub definition: Value,
    /// Gateway-authenticated author identity.
    pub author: &'a str,
    /// Author-supplied revision message.
    pub message: &'a str,
    /// Required branch head for updates.
    pub expected_revision_id: Option<&'a str>,
    /// Whether an existing pipeline must make the operation fail.
    pub create_only: bool,
}

/// Input for optimistic ontology creation or replacement.
pub struct PutOntology<'a> {
    /// Gateway-authorized tenant identity.
    pub tenant_id: &'a str,
    /// Stable tenant-local ontology identity.
    pub ontology_id: &'a str,
    /// Human-readable ontology name.
    pub display_name: &'a str,
    /// Candidate ontology document.
    pub ontology: Value,
    /// Gateway-authenticated author identity.
    pub author: &'a str,
    /// Author-supplied revision message.
    pub message: &'a str,
    /// Required branch head for updates.
    pub expected_revision_id: Option<&'a str>,
    /// Whether an existing ontology must make the operation fail.
    pub create_only: bool,
}

#[derive(Debug)]
/// Ontology record paired with its immutable lakeFS revision.
pub struct OntologyView {
    /// Immutable revision containing the ontology record.
    pub revision_id: String,
    /// Validated ontology projection.
    pub record: OntologyRecord,
}

#[derive(Debug)]
/// Reconstructible block-local ontology view pinned to pipeline state.
pub struct OntologySliceView {
    /// Production publication found at the pipeline revision.
    pub publication: OntologyPublicationRecord,
    /// Pipeline block binding found at the pipeline revision.
    pub binding: OntologyBindingRecord,
    /// Validated ontology dependency closure.
    pub ontology: Value,
}

/// Canonical Registry domain operations over a versioned storage backend.
pub struct Registry<S> {
    store: S,
}

impl<S> Registry<S>
where
    S: RevisionStore,
{
    /// Creates a Registry using the supplied versioned storage boundary.
    pub fn new(store: S) -> Self {
        Self { store }
    }

    /// Validates a bounded tenant set by checking physical S3 markers.
    pub async fn validate_tenants(
        &self,
        tenant_ids: &[String],
    ) -> Result<Vec<bool>> {
        ensure!(
            !tenant_ids.is_empty() &&
                tenant_ids.len() <= MAX_TENANT_VALIDATIONS,
            "tenant validation list must contain 1 to 100 entries"
        );
        let mut results = Vec::with_capacity(tenant_ids.len());
        for tenant_id in tenant_ids {
            ensure!(valid_tenant(tenant_id), "invalid tenant ID");
            results.push(self.store.tenant_exists(tenant_id).await?);
        }
        Ok(results)
    }

    /// Reads one existing tenant without creating storage as a side effect.
    pub async fn tenant(&self, tenant_id: &str) -> Result<TenantView> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let snapshot = self.store.load(tenant_id, None).await?;
        Ok(TenantView {
            tenant_id: tenant_id.to_owned(),
            head_revision_id: snapshot.revision_id,
        })
    }

    /// Idempotently initializes one tenant in S3 and lakeFS.
    pub async fn put_tenant(&self, tenant_id: &str) -> Result<TenantView> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let snapshot = self.store.ensure_tenant(tenant_id).await?;
        Ok(TenantView {
            tenant_id: tenant_id.to_owned(),
            head_revision_id: snapshot.revision_id,
        })
    }

    /// Permanently purges all Registry persistence for one tenant.
    pub async fn delete_tenant(
        &self,
        tenant_id: &str,
        confirmation_tenant_id: &str,
    ) -> Result<()> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        ensure!(
            tenant_id == confirmation_tenant_id,
            "tenant purge confirmation mismatch"
        );
        self.store.delete_tenant(tenant_id).await
    }

    /// Validates and commits a pipeline under optimistic concurrency.
    pub async fn put_pipeline(
        &self,
        input: PutPipeline<'_>,
    ) -> Result<PipelineView> {
        validate_identity(input.tenant_id, input.pipeline_id)?;
        ensure!(!input.name.is_empty(), "pipeline name is required");
        ensure!(!input.owner_id.is_empty(), "pipeline owner is required");
        ensure!(!input.author.is_empty(), "pipeline author is required");
        ensure!(!input.message.is_empty(), "pipeline message is required");
        self.store.ensure_tenant(input.tenant_id).await?;
        let snapshot = self.store.load(input.tenant_id, None).await?;
        let mut state = snapshot.state;
        let existing = state.pipelines.remove(input.pipeline_id);
        ensure!(
            !input.create_only || existing.is_none(),
            "pipeline already exists"
        );
        ensure!(
            input.create_only || existing.is_some(),
            "pipeline unavailable"
        );
        if let Some(expected) = input.expected_revision_id {
            ensure!(valid_revision(expected), "invalid expected revision ID");
            ensure!(snapshot.revision_id == expected, "pipeline head changed");
        } else {
            ensure!(input.create_only, "expected revision ID is required");
        }
        validate_pipeline_structure(&input.definition)?;

        let now = Utc::now().timestamp_millis();
        let created_at_ms =
            existing.as_ref().map_or(now, |record| record.created_at_ms);
        let published_revision_id =
            existing.and_then(|record| record.published_revision_id);
        let record = PipelineRecord {
            pipeline_id: input.pipeline_id.to_owned(),
            name: input.name.to_owned(),
            owner_id: input.owner_id.to_owned(),
            published_revision_id,
            definition: input.definition,
            author_id: input.author.to_owned(),
            message: input.message.to_owned(),
            created_at_ms,
            updated_at_ms: now,
            deleted_at_ms: None,
        };
        state.pipelines.insert(input.pipeline_id.to_owned(), record);
        let revision = self
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
            .context("committed pipeline missing from state")?;
        Ok(PipelineView {
            head_revision_id: revision,
            record,
        })
    }

    /// Loads one current non-deleted pipeline.
    pub async fn current_pipeline(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<PipelineView> {
        validate_identity(tenant_id, pipeline_id)?;
        let snapshot = self.store.load(tenant_id, None).await?;
        pipeline_from_snapshot(snapshot, pipeline_id)
    }

    /// Lists current non-deleted pipelines at one tenant branch head.
    pub async fn list_pipelines(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<PipelineView>> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let snapshot = self.store.load(tenant_id, None).await?;
        Ok(snapshot
            .state
            .pipelines
            .into_values()
            .filter(|pipeline| pipeline.deleted_at_ms.is_none())
            .take(limit.min(100))
            .map(|record| PipelineView {
                head_revision_id: snapshot.revision_id.clone(),
                record,
            })
            .collect())
    }

    /// Publishes a validated immutable pipeline revision for execution.
    pub async fn publish_pipeline(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineView> {
        validate_identity(tenant_id, pipeline_id)?;
        ensure!(valid_revision(revision_id), "invalid revision ID");
        let pinned = self.store.load(tenant_id, Some(revision_id)).await?;
        let candidate = pinned
            .state
            .pipelines
            .get(pipeline_id)
            .filter(|pipeline| pipeline.deleted_at_ms.is_none())
            .context("pipeline unavailable at revision")?;
        let bindings = bindings_for(&pinned.state, pipeline_id);
        validate_pipeline(&candidate.definition, &bindings)?;
        validate_pipeline_ontology_dependencies(&pinned.state, pipeline_id)?;

        let current = self.store.load(tenant_id, None).await?;
        ensure!(current.revision_id == revision_id, "pipeline head changed");
        let mut state = current.state;
        {
            let pipeline = state
                .pipelines
                .get_mut(pipeline_id)
                .context("pipeline unavailable")?;
            pipeline.published_revision_id = Some(revision_id.to_owned());
            pipeline.updated_at_ms = Utc::now().timestamp_millis();
        }
        let author = state
            .pipelines
            .get(pipeline_id)
            .context("pipeline unavailable")?
            .author_id
            .as_str();
        let head = self
            .store
            .commit(tenant_id, revision_id, &state, author, "Publish pipeline")
            .await?;
        let record = state
            .pipelines
            .remove(pipeline_id)
            .context("committed pipeline missing from state")?;
        Ok(PipelineView {
            head_revision_id: head,
            record,
        })
    }

    /// Soft-deletes a pipeline and clears its production pointer.
    pub async fn delete_pipeline(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        expected_revision_id: &str,
    ) -> Result<String> {
        validate_identity(tenant_id, pipeline_id)?;
        let snapshot = self.store.load(tenant_id, None).await?;
        ensure!(
            snapshot.revision_id == expected_revision_id,
            "pipeline head changed"
        );
        let mut state = snapshot.state;
        let pipeline = state
            .pipelines
            .get_mut(pipeline_id)
            .context("pipeline unavailable")?;
        pipeline.deleted_at_ms = Some(Utc::now().timestamp_millis());
        pipeline.published_revision_id = None;
        self.store
            .commit(
                tenant_id,
                expected_revision_id,
                &state,
                "registry",
                "Delete pipeline",
            )
            .await
    }

    /// Reconstructs a pipeline from an explicitly pinned immutable revision.
    pub async fn runtime_pipeline(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineRecord> {
        validate_identity(tenant_id, pipeline_id)?;
        ensure!(valid_revision(revision_id), "invalid revision ID");
        let mut snapshot =
            self.store.load(tenant_id, Some(revision_id)).await?;
        validate_pipeline_ontology_dependencies(&snapshot.state, pipeline_id)?;
        snapshot
            .state
            .pipelines
            .remove(pipeline_id)
            .filter(|pipeline| pipeline.deleted_at_ms.is_none())
            .context("pipeline unavailable at revision")
    }

    /// Resolves production pipelines to their immutable published revisions.
    pub async fn list_published_pipelines(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<PipelineView>> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let current = self.store.load(tenant_id, None).await?;
        let published: Vec<(String, String)> = current
            .state
            .pipelines
            .into_values()
            .filter_map(|pipeline| {
                pipeline
                    .published_revision_id
                    .map(|revision| (pipeline.pipeline_id, revision))
            })
            .take(limit.min(100))
            .collect();
        let mut result = Vec::with_capacity(published.len());
        for (pipeline_id, revision) in published {
            let record = self
                .runtime_pipeline(tenant_id, &pipeline_id, &revision)
                .await?;
            result.push(PipelineView {
                head_revision_id: revision,
                record,
            });
        }
        Ok(result)
    }

    /// Validates the complete ontology graph and commits it atomically.
    pub async fn put_ontology(
        &self,
        input: PutOntology<'_>,
    ) -> Result<OntologyView> {
        validate_identity(input.tenant_id, input.ontology_id)?;
        ensure!(
            !input.display_name.is_empty(),
            "ontology display name is required"
        );
        ensure!(!input.author.is_empty(), "ontology author is required");
        ensure!(!input.message.is_empty(), "ontology message is required");
        validate_ontology(&input.ontology)?;
        self.store.ensure_tenant(input.tenant_id).await?;
        let snapshot = self.store.load(input.tenant_id, None).await?;
        let mut state = snapshot.state;
        let existing = state.ontologies.remove(input.ontology_id);
        ensure!(
            !input.create_only || existing.is_none(),
            "ontology already exists"
        );
        ensure!(
            input.create_only || existing.is_some(),
            "ontology unavailable"
        );
        if let Some(expected) = input.expected_revision_id {
            ensure!(snapshot.revision_id == expected, "ontology head changed");
        } else {
            ensure!(input.create_only, "expected revision ID is required");
        }
        let record = OntologyRecord {
            ontology_id: input.ontology_id.to_owned(),
            display_name: input.display_name.to_owned(),
            ontology: input.ontology,
            author: input.author.to_owned(),
            message: input.message.to_owned(),
            created_at_ms: Utc::now().timestamp_millis(),
            production_publication: existing
                .and_then(|ontology| ontology.production_publication),
        };
        state
            .ontologies
            .insert(input.ontology_id.to_owned(), record);
        let revision = self
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
            .ontologies
            .remove(input.ontology_id)
            .context("committed ontology missing from state")?;
        Ok(OntologyView {
            revision_id: revision,
            record,
        })
    }

    /// Reconstructs and revalidates an ontology at an immutable revision.
    pub async fn ontology_at(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        revision_id: &str,
    ) -> Result<OntologyView> {
        validate_identity(tenant_id, ontology_id)?;
        ensure!(valid_revision(revision_id), "invalid revision ID");
        let mut snapshot =
            self.store.load(tenant_id, Some(revision_id)).await?;
        let record = snapshot
            .state
            .ontologies
            .remove(ontology_id)
            .context("ontology unavailable at revision")?;
        validate_ontology(&record.ontology)?;
        Ok(OntologyView {
            revision_id: snapshot.revision_id,
            record,
        })
    }

    /// Loads and revalidates one ontology from the current branch head.
    pub async fn current_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
    ) -> Result<OntologyView> {
        validate_identity(tenant_id, ontology_id)?;
        let mut snapshot = self.store.load(tenant_id, None).await?;
        let record = snapshot
            .state
            .ontologies
            .remove(ontology_id)
            .context("ontology unavailable")?;
        validate_ontology(&record.ontology)?;
        Ok(OntologyView {
            revision_id: snapshot.revision_id,
            record,
        })
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "publication provenance is explicit"
    )]
    /// Publishes a previously validated immutable ontology revision.
    pub async fn publish_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        display_name: &str,
        publication_id: &str,
        revision_id: &str,
        metadata: Value,
        create_only: bool,
    ) -> Result<OntologyPublicationRecord> {
        validate_identity(tenant_id, ontology_id)?;
        ensure!(valid_identity(publication_id), "invalid publication ID");
        let pinned = self
            .ontology_at(tenant_id, ontology_id, revision_id)
            .await?;
        let current = self.store.load(tenant_id, None).await?;
        let existing = current
            .state
            .ontologies
            .get(ontology_id)
            .context("ontology unavailable")?;
        ensure!(
            !create_only || existing.production_publication.is_none(),
            "ontology already published"
        );
        let hash = content_hash(&pinned.record.ontology)?;
        let version = pinned
            .record
            .ontology
            .get("version")
            .and_then(Value::as_str)
            .context("ontology version missing")?;
        let publication = OntologyPublicationRecord {
            publication_id: publication_id.to_owned(),
            revision_id: revision_id.to_owned(),
            lifecycle: "production".to_owned(),
            metadata,
            base_version: version.to_owned(),
            base_hash: hash.clone(),
            effective_hash: hash,
            author: pinned.record.author.clone(),
            message: pinned.record.message.clone(),
            published_at_ms: Utc::now().timestamp_millis(),
            retired_at_ms: None,
        };
        let mut state = current.state;
        state.ontologies.insert(
            ontology_id.to_owned(),
            OntologyRecord {
                display_name: display_name.to_owned(),
                production_publication: Some(publication),
                ..pinned.record
            },
        );
        let author = state
            .ontologies
            .get(ontology_id)
            .and_then(|ontology| ontology.production_publication.as_ref())
            .context("ontology publication unavailable")?
            .author
            .as_str();
        self.store
            .commit(
                tenant_id,
                &current.revision_id,
                &state,
                author,
                "Publish ontology",
            )
            .await?;
        state
            .ontologies
            .get_mut(ontology_id)
            .and_then(|ontology| ontology.production_publication.take())
            .context("committed ontology publication missing from state")
    }

    /// Retires the matching production ontology pointer atomically.
    pub async fn retire_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        publication_id: &str,
        revision_id: &str,
    ) -> Result<String> {
        validate_identity(tenant_id, ontology_id)?;
        let snapshot = self.store.load(tenant_id, None).await?;
        let mut state = snapshot.state;
        let publication = state
            .ontologies
            .get_mut(ontology_id)
            .and_then(|ontology| ontology.production_publication.as_mut())
            .context("ontology publication unavailable")?;
        ensure!(
            publication.publication_id == publication_id &&
                publication.revision_id == revision_id &&
                publication.lifecycle == "production",
            "ontology publication changed"
        );
        publication.lifecycle = "retired".to_owned();
        publication.retired_at_ms = Some(Utc::now().timestamp_millis());
        self.store
            .commit(
                tenant_id,
                &snapshot.revision_id,
                &state,
                "registry",
                "Retire ontology",
            )
            .await
    }

    /// Lists current ontology catalog records without exposing history.
    pub async fn list_ontologies(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyRecord>> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        let snapshot = self.store.load(tenant_id, None).await?;
        Ok(snapshot
            .state
            .ontologies
            .into_values()
            .take(limit.min(100))
            .collect())
    }

    /// Validates and commits one pipeline block ontology binding.
    pub async fn put_binding(
        &self,
        tenant_id: &str,
        expected_revision_id: &str,
        binding: OntologyBindingRecord,
    ) -> Result<(OntologyBindingRecord, String)> {
        validate_identity(tenant_id, &binding.pipeline_id)?;
        ensure!(valid_identity(&binding.block_id), "invalid block ID");
        ensure!(valid_identity(&binding.ontology_id), "invalid ontology ID");
        self.store.ensure_tenant(tenant_id).await?;
        let snapshot = self.store.load(tenant_id, None).await?;
        ensure!(
            snapshot.revision_id == expected_revision_id,
            "registry revision changed"
        );
        let pipeline = snapshot
            .state
            .pipelines
            .get(&binding.pipeline_id)
            .context("pipeline unavailable")?;
        let block_exists = pipeline
            .definition
            .get("pipeline")
            .and_then(Value::as_array)
            .is_some_and(|steps| {
                steps.iter().any(|step| {
                    step.get("step").and_then(Value::as_str) ==
                        Some(&binding.block_id)
                })
            });
        ensure!(block_exists, "pipeline block unavailable");
        let publication = snapshot
            .state
            .ontologies
            .get(&binding.ontology_id)
            .and_then(|ontology| ontology.production_publication.as_ref())
            .filter(|publication| publication.lifecycle == "production")
            .context("ontology is not published")?;
        let published = self
            .ontology_at(
                tenant_id,
                &binding.ontology_id,
                &publication.revision_id,
            )
            .await?;
        validate_ontology_selector(
            &published.record.ontology,
            &binding.resource_ids,
            &binding.resource_kinds,
        )?;
        let mut state = snapshot.state;
        let key =
            TenantState::binding_key(&binding.pipeline_id, &binding.block_id);
        state.bindings.insert(key.clone(), binding);
        let revision = self
            .store
            .commit(
                tenant_id,
                expected_revision_id,
                &state,
                "registry",
                "Update ontology binding",
            )
            .await?;
        let binding = state
            .bindings
            .remove(&key)
            .context("committed binding missing from state")?;
        Ok((binding, revision))
    }

    /// Lists current or revision-pinned ontology bindings.
    pub async fn list_bindings(
        &self,
        tenant_id: &str,
        pipeline_id: Option<&str>,
        revision_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<OntologyBindingRecord>> {
        ensure!(valid_tenant(tenant_id), "invalid tenant ID");
        if let Some(id) = pipeline_id {
            ensure!(valid_identity(id), "invalid pipeline ID");
        }
        let snapshot = self.store.load(tenant_id, revision_id).await?;
        Ok(snapshot
            .state
            .bindings
            .into_values()
            .filter(|binding| {
                pipeline_id.is_none_or(|id| binding.pipeline_id == id)
            })
            .take(limit.min(100))
            .collect())
    }

    /// Rebuilds a dependency-closed ontology slice at a pipeline revision.
    pub async fn ontology_slice(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        pipeline_revision_id: &str,
        block_id: &str,
    ) -> Result<OntologySliceView> {
        validate_identity(tenant_id, pipeline_id)?;
        ensure!(valid_identity(block_id), "invalid block ID");
        let mut snapshot = self
            .store
            .load(tenant_id, Some(pipeline_revision_id))
            .await?;
        ensure!(
            snapshot.state.pipelines.contains_key(pipeline_id),
            "pipeline unavailable at revision"
        );
        let binding = snapshot
            .state
            .bindings
            .remove(&TenantState::binding_key(pipeline_id, block_id))
            .context("ontology binding unavailable at revision")?;
        let publication = snapshot
            .state
            .ontologies
            .get_mut(&binding.ontology_id)
            .and_then(|ontology| ontology.production_publication.take())
            .filter(|publication| publication.lifecycle == "production")
            .context("ontology publication unavailable at revision")?;
        let ontology = self
            .ontology_at(
                tenant_id,
                &binding.ontology_id,
                &publication.revision_id,
            )
            .await?
            .record
            .ontology;
        let sliced = slice_ontology(
            &ontology,
            &binding.resource_ids,
            &binding.resource_kinds,
            binding.include_dependencies,
        )?;
        Ok(OntologySliceView {
            publication,
            binding,
            ontology: sliced,
        })
    }

    /// Computes a canonical semantic diff between two immutable revisions.
    pub async fn diff_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        base_revision_id: &str,
        target_revision_id: &str,
    ) -> Result<Vec<SemanticChange>> {
        let base = self
            .ontology_at(tenant_id, ontology_id, base_revision_id)
            .await?;
        let target = self
            .ontology_at(tenant_id, ontology_id, target_revision_id)
            .await?;
        semantic_diff(&base.record.ontology, &target.record.ontology)
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "three-way merge provenance is explicit"
    )]
    /// Commits a conflict-free semantic merge without lakeFS file merging.
    pub async fn merge_ontology(
        &self,
        tenant_id: &str,
        ontology_id: &str,
        base_revision_id: &str,
        left_revision_id: &str,
        right_revision_id: &str,
        expected_revision_id: &str,
        author: &str,
        message: &str,
    ) -> Result<(MergeResult, Option<OntologyView>)> {
        self.store.ensure_tenant(tenant_id).await?;
        let base = self
            .ontology_at(tenant_id, ontology_id, base_revision_id)
            .await?;
        let left = self
            .ontology_at(tenant_id, ontology_id, left_revision_id)
            .await?;
        let right = self
            .ontology_at(tenant_id, ontology_id, right_revision_id)
            .await?;
        let result = merge_ontology(
            &base.record.ontology,
            &left.record.ontology,
            &right.record.ontology,
        )?;
        let Some(ontology) = result.ontology.as_ref() else {
            return Ok((result, None));
        };
        let view = self
            .put_ontology(PutOntology {
                tenant_id,
                ontology_id,
                display_name: &left.record.display_name,
                ontology: ontology.clone(),
                author,
                message,
                expected_revision_id: Some(expected_revision_id),
                create_only: false,
            })
            .await?;
        Ok((result, Some(view)))
    }
}

fn pipeline_from_snapshot(
    snapshot: Snapshot,
    pipeline_id: &str,
) -> Result<PipelineView> {
    let mut state = snapshot.state;
    let record = state
        .pipelines
        .remove(pipeline_id)
        .filter(|pipeline| pipeline.deleted_at_ms.is_none())
        .context("pipeline unavailable")?;
    Ok(PipelineView {
        head_revision_id: snapshot.revision_id,
        record,
    })
}

fn bindings_for<'a>(
    state: &'a TenantState,
    pipeline_id: &str,
) -> Vec<PipelineBinding<'a>> {
    state
        .bindings
        .values()
        .filter(|binding| binding.pipeline_id == pipeline_id)
        .map(|binding| PipelineBinding {
            block_id: &binding.block_id,
            ontology_id: &binding.ontology_id,
        })
        .collect()
}

fn validate_pipeline_ontology_dependencies(
    state: &TenantState,
    pipeline_id: &str,
) -> Result<()> {
    let pipeline = state
        .pipelines
        .get(pipeline_id)
        .context("pipeline unavailable")?;
    let bindings = bindings_for(state, pipeline_id);
    validate_pipeline(&pipeline.definition, &bindings)?;
    for binding in state
        .bindings
        .values()
        .filter(|binding| binding.pipeline_id == pipeline_id)
    {
        let publication = state
            .ontologies
            .get(&binding.ontology_id)
            .and_then(|ontology| ontology.production_publication.as_ref())
            .filter(|publication| publication.lifecycle == "production");
        ensure!(publication.is_some(), "unpublished ontology dependency");
    }
    Ok(())
}

fn content_hash(value: &Value) -> Result<String> {
    let canonical = canonicalize(value);
    let payload = serde_json::to_vec(&canonical)?;
    Ok(format!("{:x}", Sha256::digest(payload)))
}

fn canonicalize(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .map(|(key, value)| (key.clone(), canonicalize(value)))
                .collect(),
        ),
        Value::Array(items) => {
            Value::Array(items.iter().map(canonicalize).collect())
        },
        _ => value.clone(),
    }
}

fn validate_identity(tenant_id: &str, resource_id: &str) -> Result<()> {
    ensure!(valid_tenant(tenant_id), "invalid tenant ID");
    ensure!(valid_identity(resource_id), "invalid resource ID");
    Ok(())
}

fn valid_identity(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 128 &&
        value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() ||
                matches!(byte, b'_' | b'-' | b'.' | b':')
        })
}
