//! Registry domain service over immutable tenant revisions.

use anyhow::{Context, Result, ensure};
use chrono::Utc;
use serde_json::Value;

use crate::semantics::{
    PipelineBinding, validate_pipeline, validate_pipeline_structure,
};
use crate::state::{PipelineRecord, TenantState};
use crate::storage::{
    RevisionStore, Snapshot, TenantPage, valid_revision, valid_tenant,
    validate_discovery_page,
};

const MAX_TENANT_VALIDATIONS: usize = 100;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum PipelineValidation {
    Draft,
    Release,
    Runtime,
}

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

/// Canonical Registry domain operations over a versioned storage backend.
pub struct Registry<S> {
    pub(crate) store: S,
    pub(crate) gol: Option<crate::gol_service::GolPublicationConfig>,
    pub(crate) gol_locks: [tokio::sync::Mutex<()>; 64],
}

impl<S> Registry<S>
where
    S: RevisionStore,
{
    /// Creates a Registry using the supplied versioned storage boundary.
    pub fn new(store: S) -> Self {
        Self {
            store,
            gol: None,
            gol_locks: std::array::from_fn(|_| tokio::sync::Mutex::new(())),
        }
    }

    /// Enumerates initialized tenants for authenticated runtime discovery.
    pub async fn list_tenants(
        &self,
        limit: u32,
        page_token: Option<&str>,
    ) -> Result<TenantPage> {
        validate_discovery_page(limit, page_token)?;
        self.store.list_tenants(limit, page_token).await
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
        ensure!(
            !existing
                .as_ref()
                .is_some_and(|record| record.authoring.enabled),
            "managed pipeline requires explicit authoring commands"
        );
        validate_pipeline_structure(&input.definition)?;
        self.compile_composition(
            input.tenant_id,
            input.pipeline_id,
            &snapshot.revision_id,
            &input.definition,
            PipelineValidation::Draft,
        )
        .await?;

        let now = Utc::now().timestamp_millis();
        let created_at_ms =
            existing.as_ref().map_or(now, |record| record.created_at_ms);
        let previous_revision_id =
            existing.as_ref().map(|_| snapshot.revision_id.clone());
        let published_revision_id =
            existing.and_then(|record| record.published_revision_id);
        let record = PipelineRecord {
            authoring: Default::default(),
            previous_revision_id,
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

    /// Reads authored references without exposing compiled dependency
    /// contents.
    pub async fn pipeline_revision(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineView> {
        validate_identity(tenant_id, pipeline_id)?;
        ensure!(valid_revision(revision_id), "invalid revision ID");
        let snapshot = self.store.load(tenant_id, Some(revision_id)).await?;
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
        ensure!(
            !candidate.authoring.enabled,
            "managed pipeline requires explicit approval and deployment"
        );
        self.validate_pipeline_bindings(
            tenant_id,
            &pinned.state,
            pipeline_id,
            &candidate.definition,
            PipelineValidation::Release,
        )
        .await?;
        self.compile_composition(
            tenant_id,
            pipeline_id,
            revision_id,
            &candidate.definition,
            PipelineValidation::Release,
        )
        .await?;

        let current = self.store.load(tenant_id, None).await?;
        ensure!(current.revision_id == revision_id, "pipeline head changed");
        let mut state = current.state;
        {
            let pipeline = state
                .pipelines
                .get_mut(pipeline_id)
                .context("pipeline unavailable")?;
            pipeline.published_revision_id = Some(revision_id.to_owned());
            pipeline.previous_revision_id = Some(current.revision_id.clone());
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
        state
            .bindings
            .retain(|_, binding| binding.pipeline_id != pipeline_id);
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

    /// Admits a root pipeline only at its current deployed revision.
    pub async fn runtime_pipeline(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineRecord> {
        validate_identity(tenant_id, pipeline_id)?;
        ensure!(valid_revision(revision_id), "invalid revision ID");
        let current = self.store.load(tenant_id, None).await?;
        let deployed =
            current
                .state
                .pipelines
                .get(pipeline_id)
                .is_some_and(|record| {
                    record.deleted_at_ms.is_none() &&
                        record.published_revision_id.as_deref() ==
                            Some(revision_id)
                });
        if !deployed {
            tracing::debug!(
                event.name = "registry.pipeline.deployment_unavailable"
            );
            anyhow::bail!("pipeline deployment unavailable");
        }
        self.compile_pipeline_revision(
            tenant_id,
            pipeline_id,
            revision_id,
            PipelineValidation::Runtime,
        )
        .await
    }

    /// Compiles a revision selected by a deployment snapshot or a release
    /// check.
    pub(crate) async fn compile_pipeline_revision(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
        validation: PipelineValidation,
    ) -> Result<PipelineRecord> {
        let mut snapshot =
            self.store.load(tenant_id, Some(revision_id)).await?;
        let mut record = snapshot
            .state
            .pipelines
            .remove(pipeline_id)
            .filter(|pipeline| pipeline.deleted_at_ms.is_none())
            .context("pipeline unavailable at revision")?;
        self.validate_pipeline_bindings(
            tenant_id,
            &snapshot.state,
            pipeline_id,
            &record.definition,
            validation,
        )
        .await?;
        record.definition = self
            .compile_composition(
                tenant_id,
                pipeline_id,
                revision_id,
                &record.definition,
                validation,
            )
            .await?;
        Ok(record)
    }

    /// Loads each pinned dependency once; expansion still charges repeated
    /// uses.
    pub(crate) async fn compile_composition(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
        definition: &Value,
        validation: PipelineValidation,
    ) -> Result<Value> {
        let mut pending = crate::composition::references(definition)?;
        let mut definitions = std::collections::BTreeMap::new();
        while let Some(reference) = pending.pop() {
            let key =
                (reference.pipeline_id.clone(), reference.revision_id.clone());
            if definitions.contains_key(&key) {
                continue;
            }
            ensure!(
                definitions.len() < crate::composition::MAX_TRANSFERS,
                "Composition exceeds three pipeline transfers"
            );
            let snapshot = self
                .store
                .load(tenant_id, Some(&reference.revision_id))
                .await?;
            let child = snapshot
                .state
                .pipelines
                .get(&reference.pipeline_id)
                .filter(|record| record.deleted_at_ms.is_none())
                .context("Referenced pipeline unavailable")?;
            if validation != PipelineValidation::Draft {
                self.validate_pipeline_bindings(
                    tenant_id,
                    &snapshot.state,
                    &reference.pipeline_id,
                    &child.definition,
                    validation,
                )
                .await?;
            }
            pending.extend(crate::composition::references(&child.definition)?);
            definitions.insert(key, child.definition.clone());
        }
        crate::composition::compile(
            pipeline_id,
            revision_id,
            definition,
            &definitions,
        )
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
                .compile_pipeline_revision(
                    tenant_id,
                    &pipeline_id,
                    &revision,
                    PipelineValidation::Runtime,
                )
                .await?;
            result.push(PipelineView {
                head_revision_id: revision,
                record,
            });
        }
        Ok(result)
    }

    pub(crate) async fn validate_pipeline_bindings(
        &self,
        tenant: &str,
        state: &TenantState,
        pipeline_id: &str,
        definition: &Value,
        validation: PipelineValidation,
    ) -> Result<()> {
        validate_pipeline(definition, &bindings_for(state, pipeline_id))?;
        let pins = state
            .bindings
            .values()
            .filter(|binding| binding.pipeline_id == pipeline_id);
        for binding in pins.clone() {
            ensure!(
                !binding.generation.is_empty() &&
                    !binding.source_revision.is_empty() &&
                    !binding.symbols.is_empty(),
                "unpublished GOL ontology dependency"
            );
        }
        if validation == PipelineValidation::Release {
            self.check_active_gol_bindings(tenant, pins).await?;
        }
        Ok(())
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

pub(crate) fn validate_identity(
    tenant_id: &str,
    resource_id: &str,
) -> Result<()> {
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
