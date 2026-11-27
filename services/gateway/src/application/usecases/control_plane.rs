//! Authenticated GOL lifecycle operations and separately authorized
//! disclosures.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};

use crate::application::ports::audit_store::{AuditEvent, AuditFilter};
use crate::application::ports::control_plane_store::{
    ControlPlaneStore, GolDisclosure, GolPackageOrigin, GolPublication,
    GolSummary, GolValidation, GolWorkspace, GolWrite, PipelineExecution,
    PipelineOntologyBinding, PluginGolPackage,
};
use crate::application::ports::conversation_agent::ConversationAgent;
use crate::application::usecases::audit::{
    AuditAction, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;

const HARD_LIMIT: usize = 100;

/// Registry remains the only source compiler and active-generation authority.
pub struct ControlPlaneService {
    store: Arc<dyn ControlPlaneStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
    disclosure: Arc<dyn GolDisclosure>,
    plugin_catalog: Option<Arc<dyn ConversationAgent>>,
}

impl ControlPlaneService {
    /// Keeps UI hints as restrictive as the live source-authoring boundary.
    pub async fn gol_can_manage(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: Option<&str>,
        permission: Permission,
    ) -> Result<bool> {
        if !self
            .ontology_permission(tenant, user, context, id, permission)
            .await?
        {
            return Ok(false);
        }
        let admitted = match id {
            Some(id) => {
                self.disclosure
                    .admit_source(tenant, user, context, id, None)
                    .await
            },
            None if permission == Permission::CreateOntology => {
                self.disclosure
                    .admit_creation(tenant, user, context, "_new")
                    .await
            },
            None => bail!("Invalid GOL management permission"),
        };
        match admitted {
            Ok(()) => Ok(true),
            Err(_) => {
                tracing::debug!(event.name = "gol.management_hint.denied");
                Ok(false)
            },
        }
    }

    /// Requires the same shared disclosure boundary for every GOL operation.
    pub fn new(
        store: Arc<dyn ControlPlaneStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
        disclosure: Arc<dyn GolDisclosure>,
    ) -> Self {
        Self {
            store,
            identity,
            auth,
            audit,
            disclosure,
            plugin_catalog: None,
        }
    }

    /// Connects the deployment inventory independently of chat availability.
    pub fn with_plugin_catalog(
        mut self,
        catalog: Arc<dyn ConversationAgent>,
    ) -> Self {
        self.plugin_catalog = Some(catalog);
        self
    }

    async fn deployed_gol_packages(&self) -> Result<Vec<PluginGolPackage>> {
        let Some(catalog) = &self.plugin_catalog else {
            return Ok(Vec::new());
        };
        let packages = catalog.plugin_ontologies().await?;
        let mut ids = HashSet::with_capacity(packages.len());
        for package in &packages {
            Self::validate_ontology_id(&package.id)?;
            package.origin().validate()?;
            ensure!(
                ids.insert(package.id.as_str()),
                "duplicate deployed GOL ontology identity"
            );
        }
        Ok(packages)
    }

    /// Package discovery grants neither source access nor publication.
    pub async fn gol_ontology_packages(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
    ) -> Result<Vec<PluginGolPackage>> {
        if !self
            .gol_can_manage(
                tenant,
                user,
                context,
                None,
                Permission::CreateOntology,
            )
            .await?
        {
            return Ok(Vec::new());
        }
        self.deployed_gol_packages().await
    }

    /// Creates or upgrades a tenant draft from a deployed package.
    #[tracing::instrument(name = "galadril.gol.package.install", skip_all, fields(galadril.error.code = tracing::field::Empty))]
    pub async fn install_gol_ontology_package(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        ontology_id: &str,
        origin: &GolPackageOrigin,
        expected_revision: Option<&str>,
    ) -> Result<GolSummary> {
        Self::validate_ontology_id(ontology_id)?;
        origin.validate()?;
        ensure!(
            self.ontology_permission(
                tenant,
                user,
                context,
                None,
                Permission::CreateOntology
            )
            .await?,
            "GOL operation denied"
        );
        self.disclosure
            .admit_creation(tenant, user, context, ontology_id)
            .await?;
        if let Some(revision) = expected_revision {
            Self::validate_revision(revision)?;
            self.require_gol_permission(
                tenant,
                user,
                context,
                ontology_id,
                Permission::Edit,
            )
            .await?;
            self.disclosure
                .admit_source(
                    tenant,
                    user,
                    context,
                    ontology_id,
                    Some(revision),
                )
                .await?;
        }
        let package = self
            .deployed_gol_packages()
            .await?
            .into_iter()
            .find(|package| {
                package.id == ontology_id && package.origin() == *origin
            })
            .context("GOL package version unavailable")?;
        let operation = self
            .audit
            .begin(
                tenant,
                user,
                AuditTarget::new(
                    AuditAction::SaveOntologyDraft,
                    "ontology",
                    ontology_id,
                ),
                context,
            )
            .await?;
        let result = async {
            self.disclosure
                .admit_creation(tenant, user, context, ontology_id)
                .await?;
            if expected_revision.is_none() {
                // Register tenant ownership first so an interrupted install
                // remains discoverable.
                self.auth
                    .upsert_relationship(
                        "ontology",
                        &format!("{tenant}/{ontology_id}"),
                        "parent",
                        "tenant",
                        tenant,
                    )
                    .await?;
            }
            let workspace = self
                .store
                .save_gol_ontology(
                    tenant,
                    user,
                    GolWrite {
                        ontology_id,
                        display_name: &package.name,
                        files: &package.files,
                        expected_revision,
                        origin: Some(package.origin()),
                    },
                )
                .await?;
            Ok(workspace.summary)
        }
        .await;
        match result {
            Ok(summary) => {
                operation.succeeded().await?;
                galadril_telemetry::contract::emit_gol_package_install_completed(None);
                Ok(summary)
            },
            Err(error) => {
                operation.failed("gol_package_install_failed").await?;
                let code =
                    galadril_telemetry::contract::ErrorCode::Unavailable;
                tracing::Span::current()
                    .record("galadril.error.code", code.as_str());
                galadril_telemetry::contract::emit_gol_package_install_completed(Some(code));
                Err(error)
            },
        }
    }

    /// Checks a management permission without exposing source contents.
    pub async fn ontology_permission(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: Option<&str>,
        permission: Permission,
    ) -> Result<bool> {
        self.verify_user(tenant, user).await?;
        let (kind, target) = match (permission, id) {
            (Permission::CreateOntology, None) => ("tenant", tenant),
            (
                Permission::Edit | Permission::Publish | Permission::Delete,
                Some(id),
            ) => {
                Self::validate_ontology_id(id)?;
                if !self
                    .can_view(tenant, user, context, "ontology", id)
                    .await?
                {
                    return Ok(false);
                }
                ("ontology", id)
            },
            _ => bail!("Invalid GOL management permission"),
        };
        self.auth
            .is_authorized(
                user,
                tenant,
                permission,
                kind,
                target,
                Some(context),
            )
            .await
    }

    async fn require_gol_permission(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        permission: Permission,
    ) -> Result<()> {
        ensure!(
            self.ontology_permission(
                tenant,
                user,
                context,
                Some(id),
                permission
            )
            .await?,
            "GOL operation denied"
        );
        Ok(())
    }

    /// Rejects identities outside the canonical GOL qualified-name grammar.
    fn validate_ontology_id(id: &str) -> Result<()> {
        ensure!(id.len() <= 128, "invalid GOL ontology identity");
        id.parse::<galadril_ontology::OntologyId>()
            .context("invalid GOL ontology identity")?;
        Ok(())
    }

    /// Source snapshots remain opaque immutable Registry revision identifiers.
    fn validate_revision(revision: &str) -> Result<()> {
        ensure!(
            galadril_registry::storage::valid_revision(revision),
            "invalid GOL source revision"
        );
        Ok(())
    }

    /// Filters every catalog candidate through live GOL disclosure checks.
    pub async fn gol_ontologies(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<GolSummary>> {
        self.verify_user(tenant, user).await?;
        let candidates = self
            .store
            .list_gol_ontologies(tenant, bounded(limit))
            .await?;
        let mut result = Vec::with_capacity(candidates.len());
        for mut candidate in candidates {
            if self
                .can_view(
                    tenant,
                    user,
                    context,
                    "ontology",
                    &candidate.ontology_id,
                )
                .await? &&
                self.disclosure
                    .admit_catalog(tenant, user, context, &candidate)
                    .await?
            {
                if self
                    .disclosure
                    .admit_source(
                        tenant,
                        user,
                        context,
                        &candidate.ontology_id,
                        Some(&candidate.source_revision),
                    )
                    .await
                    .is_err()
                {
                    let (Some(_), Some(active_source)) = (
                        &candidate.active_generation,
                        &candidate.active_source_revision,
                    ) else {
                        continue;
                    };
                    candidate.display_name.clone_from(&candidate.ontology_id);
                    candidate.source_revision.clone_from(active_source);
                    candidate.status = "published".into();
                    candidate.origin = None;
                    tracing::debug!(event.name = "gol.catalog.draft_redacted");
                }
                result.push(candidate);
            }
        }
        Ok(result)
    }

    /// Source authoring authority is stricter than ordinary catalog
    /// visibility.
    pub async fn gol_workspace(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
    ) -> Result<GolWorkspace> {
        Self::validate_ontology_id(id)?;
        self.require_gol_permission(
            tenant,
            user,
            context,
            id,
            Permission::Edit,
        )
        .await?;
        self.disclosure
            .admit_source(tenant, user, context, id, None)
            .await?;
        self.store.gol_workspace(tenant, id).await
    }

    /// Saves unfinished source packages without introducing a Gateway
    /// compiler.
    #[tracing::instrument(skip_all, fields(operation = "gol.save"))]
    pub async fn save_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        input: GolWrite<'_>,
    ) -> Result<GolWorkspace> {
        self.verify_user(tenant, user).await?;
        Self::validate_ontology_id(input.ontology_id)?;
        galadril_registry::gol::validate_files(input.files)?;
        if let Some(revision) = input.expected_revision {
            Self::validate_revision(revision)?;
            self.require_gol_permission(
                tenant,
                user,
                context,
                input.ontology_id,
                Permission::Edit,
            )
            .await?;
        } else {
            ensure!(
                self.ontology_permission(
                    tenant,
                    user,
                    context,
                    None,
                    Permission::CreateOntology
                )
                .await?,
                "GOL operation denied"
            );
        }
        if input.expected_revision.is_some() {
            self.disclosure
                .admit_source(
                    tenant,
                    user,
                    context,
                    input.ontology_id,
                    input.expected_revision,
                )
                .await?;
        } else {
            self.disclosure
                .admit_creation(tenant, user, context, input.ontology_id)
                .await?;
        }
        let operation = self
            .audit
            .begin(
                tenant,
                user,
                AuditTarget::new(
                    AuditAction::SaveOntologyDraft,
                    "ontology",
                    input.ontology_id,
                ),
                context,
            )
            .await?;
        if input.expected_revision.is_none() {
            let resource = format!("{tenant}/{}", input.ontology_id);
            for (relation, subject_type, subject_id) in
                [("parent", "tenant", tenant), ("owner", "user", user)]
            {
                if let Err(error) = self
                    .auth
                    .upsert_relationship(
                        "ontology",
                        &resource,
                        relation,
                        subject_type,
                        subject_id,
                    )
                    .await
                {
                    operation
                        .failed("authorization_registration_failed")
                        .await?;
                    return Err(error);
                }
            }
        }
        match self.store.save_gol_ontology(tenant, user, input).await {
            Ok(workspace) => {
                operation.succeeded().await?;
                Ok(workspace)
            },
            Err(error) => {
                operation.failed("gol_source_save_failed").await?;
                Err(error)
            },
        }
    }

    /// Detailed diagnostics require the same authorization as source reads.
    pub async fn validate_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        revision: &str,
    ) -> Result<GolValidation> {
        Self::validate_ontology_id(id)?;
        Self::validate_revision(revision)?;
        self.require_gol_permission(
            tenant,
            user,
            context,
            id,
            Permission::Edit,
        )
        .await?;
        self.disclosure
            .admit_source(tenant, user, context, id, Some(revision))
            .await?;
        let approval = self
            .gol_compilation_approval(tenant, user, context, id, revision)
            .await?;
        self.store
            .validate_gol_ontology(tenant, id, revision, &approval)
            .await
    }

    /// Publishes a pinned revision; authorization never follows a cached
    /// grant.
    #[expect(
        clippy::too_many_arguments,
        reason = "Actor, source pin and expected generation define one publication boundary"
    )]
    pub async fn publish_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        revision: &str,
        expected: Option<&str>,
    ) -> Result<GolPublication> {
        Self::validate_ontology_id(id)?;
        Self::validate_revision(revision)?;
        if let Some(generation) = expected {
            generation
                .parse::<galadril_ontology::archive::remote::Generation>()?;
        }
        self.require_gol_permission(
            tenant,
            user,
            context,
            id,
            Permission::Publish,
        )
        .await?;
        self.disclosure
            .admit_source(tenant, user, context, id, Some(revision))
            .await?;
        let approval = self
            .gol_compilation_approval(tenant, user, context, id, revision)
            .await?;
        let operation = self
            .audit
            .begin(
                tenant,
                user,
                AuditTarget::new(AuditAction::PublishOntology, "ontology", id)
                    .with_revision_id(revision),
                context,
            )
            .await?;
        match self
            .store
            .publish_gol_ontology(
                tenant, user, id, revision, expected, &approval,
            )
            .await
        {
            Ok(publication) => {
                operation.succeeded().await?;
                Ok(publication)
            },
            Err(error) => {
                operation.failed("gol_publication_failed").await?;
                Err(error)
            },
        }
    }

    /// Import diagnostics are reachable only after admitting each source
    /// package.
    async fn gol_compilation_approval(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        revision: &str,
    ) -> Result<
        crate::application::ports::control_plane_store::GolCompileApproval,
    > {
        let mut approved = std::collections::BTreeSet::new();
        for _round in 0..=31 {
            let owners = approved.iter().cloned().collect::<Vec<_>>();
            let preview = self
                .store
                .preview_gol_dependencies(tenant, id, revision, &owners)
                .await?;
            anyhow::ensure!(
                preview.required_owners.len() <= 31 &&
                    preview.dependencies.len() <= 31,
                "GOL dependency approval budget exceeded"
            );
            if preview.required_owners.is_empty() {
                for dependency in &preview.dependencies {
                    self.disclosure
                        .admit_source(
                            tenant,
                            user,
                            context,
                            &dependency.ontology_id,
                            Some(&dependency.source_revision),
                        )
                        .await?;
                }
                return Ok(crate::application::ports::control_plane_store::GolCompileApproval {plan_pin:preview.plan_pin.context("GOL dependency plan unavailable")?,approved_owners:owners});
            }
            for owner in preview.required_owners {
                Self::validate_ontology_id(&owner)?;
                anyhow::ensure!(
                    !approved.contains(&owner) && approved.len() < 31,
                    "GOL dependency plan did not progress"
                );
                self.disclosure
                    .admit_source(tenant, user, context, &owner, None)
                    .await?;
                approved.insert(owner);
            }
        }
        anyhow::bail!("GOL dependency approval budget exceeded")
    }

    /// Deletion cannot bypass current permissions or source disclosure floors.
    pub async fn delete_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        expected: &str,
    ) -> Result<String> {
        Self::validate_ontology_id(id)?;
        Self::validate_revision(expected)?;
        self.require_gol_permission(
            tenant,
            user,
            context,
            id,
            Permission::Delete,
        )
        .await?;
        self.disclosure
            .admit_source(tenant, user, context, id, Some(expected))
            .await?;
        let operation = self
            .audit
            .begin(
                tenant,
                user,
                AuditTarget::new(AuditAction::DeleteOntology, "ontology", id)
                    .with_revision_id(expected),
                context,
            )
            .await?;
        match self
            .store
            .delete_gol_ontology(tenant, user, id, expected)
            .await
        {
            Ok(revision) => {
                operation.succeeded().await?;
                Ok(revision)
            },
            Err(error) => {
                operation.failed("gol_deletion_failed").await?;
                Err(error)
            },
        }
    }

    /// Binding updates require both pipeline edits and approved GOL metadata.
    pub async fn authorize_ontology_binding(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        pipeline: &str,
    ) -> Result<()> {
        self.verify_user(tenant, user).await?;
        Self::validate_ontology_id(id)?;
        ensure!(
            self.auth
                .is_authorized(
                    user,
                    tenant,
                    Permission::Edit,
                    "pipeline",
                    pipeline,
                    Some(context)
                )
                .await?,
            "Pipeline operation denied"
        );
        ensure!(
            self.can_view(tenant, user, context, "ontology", id).await?,
            "GOL operation denied"
        );
        self.disclosure
            .admit_source(tenant, user, context, id, None)
            .await?;
        ensure!(
            self.store
                .list_gol_ontologies(tenant, 100)
                .await?
                .iter()
                .any(|entry| entry.ontology_id == id &&
                    entry.active_generation.is_some()),
            "GOL ontology must be published before binding"
        );
        Ok(())
    }

    /// Captures a generation without activating an unrelated pipeline draft.
    #[expect(
        clippy::too_many_arguments,
        reason = "Binding actor and immutable pipeline selection are explicit"
    )]
    pub async fn bind_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        pipeline: &str,
        block: &str,
        ontology: &str,
        expected: &str,
    ) -> Result<String> {
        self.authorize_ontology_binding(
            tenant, user, context, ontology, pipeline,
        )
        .await?;
        self.store
            .bind_ontology(tenant, user, pipeline, block, ontology, expected)
            .await
    }

    /// Rejects inactive or cross-tenant identities before control-plane reads.
    async fn verify_user(&self, tenant_id: &str, user_id: &str) -> Result<()> {
        self.identity.verify_user(tenant_id, user_id).await
    }

    /// Requires tenant administration through both SpiceDB and Cedar.
    async fn require_tenant_admin(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
    ) -> Result<()> {
        self.verify_user(tenant_id, user_id).await?;
        if !self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                Permission::Manage,
                "tenant",
                tenant_id,
                Some(context),
            )
            .await
            .context("Failed to authorize control-plane administration")?
        {
            bail!("Authorization denied");
        }
        Ok(())
    }

    /// Applies resource view authorization with trusted contextual facts.
    async fn can_view(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        resource_type: &str,
        resource_id: &str,
    ) -> Result<bool> {
        self.auth
            .is_authorized(
                user_id,
                tenant_id,
                Permission::View,
                resource_type,
                resource_id,
                Some(context),
            )
            .await
    }

    /// Lists immutable tenant audit history for administrators.
    pub async fn audit_events(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        filter: &AuditFilter,
        limit: usize,
    ) -> Result<Vec<AuditEvent>> {
        self.require_tenant_admin(tenant_id, user_id, context)
            .await?;
        self.audit.list(tenant_id, filter, bounded(limit)).await
    }

    /// Lists ontology bindings only for visible ontologies and pipelines.
    pub async fn ontology_bindings(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>> {
        self.verify_user(tenant_id, user_id).await?;
        if let Some(pipeline_id) = pipeline_id &&
            !self
                .can_view(tenant_id, user_id, context, "pipeline", pipeline_id)
                .await?
        {
            bail!("Authorization denied");
        }
        let candidates = self
            .store
            .list_ontology_bindings(tenant_id, pipeline_id, bounded(limit))
            .await?;
        let mut allowed = Vec::with_capacity(candidates.len());
        let mut pipeline_decisions = HashMap::new();
        for binding in candidates {
            let pipeline_allowed = if pipeline_id.is_some() {
                true
            } else if let Some(decision) =
                pipeline_decisions.get(&binding.pipeline_id)
            {
                *decision
            } else {
                let decision = self
                    .can_view(
                        tenant_id,
                        user_id,
                        context,
                        "pipeline",
                        &binding.pipeline_id,
                    )
                    .await?;
                pipeline_decisions
                    .insert(binding.pipeline_id.clone(), decision);
                decision
            };
            if pipeline_allowed &&
                self.can_view(
                    tenant_id,
                    user_id,
                    context,
                    "ontology",
                    &binding.ontology_id,
                )
                .await?
            {
                match self
                    .disclosure
                    .admit_source(
                        tenant_id,
                        user_id,
                        context,
                        &binding.ontology_id,
                        Some(&binding.source_revision),
                    )
                    .await
                {
                    Ok(()) => allowed.push(binding),
                    Err(_) => tracing::debug!(
                        event.name = "gol.binding.disclosure_denied"
                    ),
                }
            }
        }
        Ok(allowed)
    }

    /// Lists durable executions only for visible pipelines.
    pub async fn pipeline_executions(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineExecution>> {
        self.verify_user(tenant_id, user_id).await?;
        if let Some(pipeline_id) = pipeline_id &&
            !self
                .can_view(tenant_id, user_id, context, "pipeline", pipeline_id)
                .await?
        {
            bail!("Authorization denied");
        }
        let candidates = self
            .store
            .list_pipeline_executions(tenant_id, pipeline_id, bounded(limit))
            .await?;
        if pipeline_id.is_some() {
            return Ok(candidates);
        }

        let mut decisions = HashMap::new();
        let mut allowed = Vec::with_capacity(candidates.len());
        for execution in candidates {
            let decision = if let Some(decision) =
                decisions.get(&execution.pipeline_id)
            {
                *decision
            } else {
                let decision = self
                    .can_view(
                        tenant_id,
                        user_id,
                        context,
                        "pipeline",
                        &execution.pipeline_id,
                    )
                    .await?;
                decisions.insert(execution.pipeline_id.clone(), decision);
                decision
            };
            if decision {
                allowed.push(execution);
            }
        }
        Ok(allowed)
    }
}

const fn bounded(limit: usize) -> usize {
    if limit < 1 {
        1
    } else if limit > HARD_LIMIT {
        HARD_LIMIT
    } else {
        limit
    }
}

#[cfg(test)]
#[path = "../../../tests/gol_control_plane.rs"]
mod tests;
