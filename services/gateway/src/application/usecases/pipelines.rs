//! Authorized, versioned pipeline authoring and runtime publication.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use galadril_registry::composition::{self, MAX_TRANSFERS};
use serde_json::Value;

use crate::application::ports::conversation_agent::ConversationAgent;
use crate::application::ports::pipeline_store::{
    NewPipelineRevision, PipelineDefinition, PipelineStore,
};
use crate::application::usecases::audit::{
    AuditAction, AuditOperation, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::capabilities::DiscoveryError;
use crate::application::usecases::identity::IdentityService;
use crate::domain::capabilities::{CapabilityCatalog, builtin_catalog};

const MAX_PIPELINE_ID_BYTES: usize = 128;
const MAX_NAME_BYTES: usize = 256;
const MAX_MESSAGE_BYTES: usize = 1024;

/// Keeps authorization failures distinguishable from malformed graph errors.
#[derive(Debug)]
pub struct PipelineAccessDenied;

impl std::fmt::Display for PipelineAccessDenied {
    fn fmt(
        &self,
        formatter: &mut std::fmt::Formatter<'_>,
    ) -> std::fmt::Result {
        formatter.write_str("Pipeline access denied")
    }
}

impl std::error::Error for PipelineAccessDenied {}

/// Coordinates Registry pipeline operations, authorization, and audit.
pub struct PipelineService {
    store: Arc<dyn PipelineStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
    catalog: Option<Arc<CapabilityCatalog>>,
    runtime_catalog: Option<Arc<dyn ConversationAgent>>,
}

impl PipelineService {
    /// Uses the executable inventory only after authenticating tenant
    /// membership.
    pub async fn plugin_ontologies(
        &self,
        tenant: &str,
        user: &str,
    ) -> Result<Value> {
        self.identity
            .verify_user(tenant, user)
            .await
            .map_err(|_| PipelineAccessDenied)?;
        self.runtime_catalog
            .as_ref()
            .context("Plugin ontology inventory unavailable")?
            .plugin_ontologies()
            .await
    }

    /// Returns stable block diagnostics while keeping dependency details
    /// private.
    pub async fn diagnose(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        definition: &Value,
    ) -> Result<Vec<galadril_registry::authoring::PipelineDiagnostic>> {
        self.identity
            .verify_user(tenant, user)
            .await
            .map_err(|_| PipelineAccessDenied)?;
        let mut issues =
            galadril_registry::authoring::diagnose_definition(definition)?;
        if issues.is_empty() &&
            let Err(error) = self
                .compose(tenant, user, context, id, definition, false)
                .await
        {
            let phase = error
                .chain()
                .map(ToString::to_string)
                .find(|phase| {
                    matches!(
                        phase.as_str(),
                        "composition_graph" | "plugin_capabilities"
                    )
                })
                .unwrap_or_else(|| {
                    "composition_authorization_or_reference".to_owned()
                });
            let transport = error.downcast_ref::<reqwest::Error>();
            tracing::warn!(
                event.name = "pipeline.composition.rejected",
                phase,
                dependency_status = transport
                    .and_then(|error| error.status())
                    .map(|status| status.as_u16()),
                dependency_timeout =
                    transport.is_some_and(reqwest::Error::is_timeout),
                "Pipeline validation failed closed"
            );
            issues.push(galadril_registry::authoring::PipelineDiagnostic {
                code: "backend".to_owned(),
                node_id: None,
                field: None,
            });
        }
        Ok(issues)
    }

    /// Authorizes each authoring transition before reading or changing
    /// Registry state.
    #[expect(
        clippy::too_many_arguments,
        reason = "explicit authenticated authoring provenance"
    )]
    #[tracing::instrument(skip_all, fields(operation = command.operation_name()))]
    pub async fn author(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        expected: Option<&str>,
        name: &str,
        definition: &Value,
        message: &str,
        command: galadril_registry::authoring::AuthoringCommand,
    ) -> Result<PipelineDefinition> {
        use galadril_registry::authoring::{
            AuthoringCommand, definition_hash, merge_definitions,
            validate_draft,
        };
        anyhow::ensure!(
            !matches!(command, AuthoringCommand::BindOntology { .. }),
            "Use the authorized ontology binding operation"
        );
        let pipeline_id = Self::validate_pipeline_id(pipeline_id)?;
        let name = Self::validate_name(name)?;
        let message = Self::validate_message(message)?;
        let permission = if expected.is_none() {
            Permission::CreatePipeline
        } else {
            match command {
                AuthoringCommand::Approve |
                AuthoringCommand::Publish |
                AuthoringCommand::Deploy => Permission::Publish,
                _ => Permission::Edit,
            }
        };
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                if expected.is_none() {
                    AuditAction::CreatePipeline
                } else {
                    AuditAction::UpdatePipeline
                },
                pipeline_id,
                permission,
                if expected.is_none() {
                    "tenant"
                } else {
                    "pipeline"
                },
                if expected.is_none() {
                    tenant_id
                } else {
                    pipeline_id
                },
                expected,
            )
            .await?;
        let result = async {
            let current = if expected.is_some() {
                self.require_pipeline(
                    tenant_id,
                    user_id,
                    context,
                    pipeline_id,
                    Permission::View,
                )
                .await?;
                Some(
                    self.store
                        .get(tenant_id, pipeline_id)
                        .await?
                        .context("Pipeline unavailable")?,
                )
            } else {
                None
            };
            if let Some(current) = &current {
                anyhow::ensure!(
                    Some(current.head_revision_id.as_str()) == expected,
                    "pipeline head changed"
                );
            }
            match &command {
                AuthoringCommand::SaveDraft { .. } => {
                    validate_draft(definition)?
                },
                AuthoringCommand::Approve | AuthoringCommand::Publish => {
                    let current =
                        current.as_ref().context("Pipeline unavailable")?;
                    Self::validate_definition(
                        &current.definition,
                        &current.name,
                    )?;
                    self.compose(
                        tenant_id,
                        user_id,
                        context,
                        pipeline_id,
                        &current.definition,
                        matches!(command, AuthoringCommand::Publish),
                    )
                    .await?;
                },
                AuthoringCommand::Deploy => {
                    self.require_pipeline(
                        tenant_id,
                        user_id,
                        context,
                        pipeline_id,
                        Permission::Execute,
                    )
                    .await?;
                    let release = current
                        .as_ref()
                        .and_then(|item| item.authoring.release.as_ref())
                        .context("Pipeline publication required")?;
                    let pinned = self
                        .store
                        .get_revision(
                            tenant_id,
                            pipeline_id,
                            &release.revision_id,
                        )
                        .await?
                        .context("Pipeline publication unavailable")?;
                    self.compose(
                        tenant_id,
                        user_id,
                        context,
                        pipeline_id,
                        &pinned.definition,
                        true,
                    )
                    .await?;
                },
                AuthoringCommand::Merge {
                    proposal_id,
                    resolutions,
                } => {
                    let current =
                        current.as_ref().context("Pipeline unavailable")?;
                    let proposal = current
                        .authoring
                        .proposals
                        .get(proposal_id)
                        .context("Pipeline proposal unavailable")?;
                    let source = current
                        .authoring
                        .branches
                        .get(&proposal.source_branch)
                        .context("Pipeline branch unavailable")?;
                    anyhow::ensure!(
                        proposal.source_hash ==
                            definition_hash(&source.definition)? &&
                            proposal.target_hash ==
                                definition_hash(&current.definition)?,
                        "pipeline proposal changed"
                    );
                    let preview = merge_definitions(
                        &source.base_definition,
                        &current.definition,
                        &source.definition,
                        resolutions,
                    )?;
                    anyhow::ensure!(
                        preview.conflicts.is_empty(),
                        "pipeline merge conflicts unresolved"
                    );
                    validate_draft(&preview.definition)?;
                },
                _ => {},
            }
            self.store
                .author(
                    tenant_id,
                    expected,
                    &NewPipelineRevision {
                        pipeline_id,
                        name,
                        owner_id: user_id,
                        definition,
                        author_id: user_id,
                        message,
                    },
                    &command,
                )
                .await
        }
        .await;
        match result {
            Ok(value) => {
                if expected.is_none() {
                    let resource = format!("{tenant_id}/{pipeline_id}");
                    for (relation, subject_type, subject) in [
                        ("parent", "tenant", tenant_id),
                        ("owner", "user", user_id),
                    ] {
                        if let Err(error) = self
                            .auth
                            .upsert_relationship(
                                "pipeline",
                                &resource,
                                relation,
                                subject_type,
                                subject,
                            )
                            .await
                        {
                            operation
                                .failed("authorization_replication_failed")
                                .await?;
                            return Err(error);
                        }
                    }
                }
                operation
                    .with_revision_id(&value.head_revision_id)
                    .succeeded()
                    .await?;
                Ok(value)
            },
            Err(error) => {
                operation.failed("authoring_rejected").await?;
                Err(error)
            },
        }
    }

    /// Checks the requested object before any storage access can disclose it.
    async fn require_pipeline(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        permission: Permission,
    ) -> Result<()> {
        if !self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                permission,
                "pipeline",
                pipeline_id,
                Some(context),
            )
            .await?
        {
            return Err(PipelineAccessDenied.into());
        }
        Ok(())
    }

    /// Reads current or pinned authored graphs without expanding hidden
    /// targets.
    #[tracing::instrument(skip_all, name = "pipeline.read")]
    pub async fn read(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        revision_id: Option<&str>,
    ) -> Result<Option<PipelineDefinition>> {
        Self::validate_pipeline_id(pipeline_id)?;
        self.identity
            .verify_user(tenant_id, user_id)
            .await
            .map_err(|_| PipelineAccessDenied)?;
        self.require_pipeline(
            tenant_id,
            user_id,
            context,
            pipeline_id,
            Permission::View,
        )
        .await?;
        let result = match revision_id {
            Some(revision) => {
                anyhow::ensure!(
                    galadril_registry::storage::valid_revision(revision),
                    "Invalid pipeline revision"
                );
                self.store
                    .get_revision(tenant_id, pipeline_id, revision)
                    .await?
            },
            None => self.store.get(tenant_id, pipeline_id).await?,
        };
        Ok(result.filter(|pipeline| {
            pipeline.pipeline_id == pipeline_id &&
                pipeline.deleted_at_ms.is_none()
        }))
    }

    /// A readable graph does not itself grant authority to run its
    /// capabilities.
    pub async fn link_targets(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<PipelineDefinition>> {
        let mut visible = Vec::new();
        for pipeline in self.list(tenant_id, user_id, context, limit).await? {
            if pipeline.published_revision_id.is_some() &&
                self.auth
                    .is_authorized(
                        user_id,
                        tenant_id,
                        Permission::Execute,
                        "pipeline",
                        &pipeline.pipeline_id,
                        Some(context),
                    )
                    .await?
            {
                visible.push(pipeline);
            }
        }
        Ok(visible)
    }

    /// Uses the same bounded compiler as Registry; previews cannot grant
    /// access.
    #[tracing::instrument(skip_all, name = "pipeline.composition.authorize")]
    pub async fn compose(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        definition: &Value,
        publishing: bool,
    ) -> Result<composition::CompiledPipeline> {
        Self::validate_pipeline_id(pipeline_id)?;
        self.identity
            .verify_user(tenant_id, user_id)
            .await
            .map_err(|_| PipelineAccessDenied)?;
        let mut pending = composition::references(definition)?;
        let mut definitions = std::collections::BTreeMap::new();
        while let Some(reference) = pending.pop() {
            let key =
                (reference.pipeline_id.clone(), reference.revision_id.clone());
            if definitions.contains_key(&key) {
                continue;
            }
            anyhow::ensure!(
                definitions.len() < MAX_TRANSFERS,
                "Composition exceeds three pipeline transfers"
            );
            for permission in [Permission::View, Permission::Execute] {
                self.require_pipeline(
                    tenant_id,
                    user_id,
                    context,
                    &reference.pipeline_id,
                    permission,
                )
                .await?;
            }
            if publishing {
                self.require_pipeline(
                    tenant_id,
                    user_id,
                    context,
                    &reference.pipeline_id,
                    Permission::Publish,
                )
                .await?;
            }
            let current = self
                .store
                .get(tenant_id, &reference.pipeline_id)
                .await?
                .filter(|item| item.deleted_at_ms.is_none())
                .context("Referenced pipeline unavailable")?;
            anyhow::ensure!(
                current.published_revision_id.as_deref() ==
                    Some(reference.revision_id.as_str()),
                "Select the current published pipeline revision"
            );
            let pinned = self
                .store
                .get_revision(
                    tenant_id,
                    &reference.pipeline_id,
                    &reference.revision_id,
                )
                .await?
                .context("Referenced pipeline revision unavailable")?;
            anyhow::ensure!(
                pinned.pipeline_id == reference.pipeline_id &&
                    pinned.head_revision_id == reference.revision_id &&
                    pinned.deleted_at_ms.is_none(),
                "Referenced pipeline identity mismatch"
            );
            pending.extend(composition::references(&pinned.definition)?);
            definitions.insert(key, pinned.definition);
        }
        let compiled = composition::compile_with_stats(
            pipeline_id,
            "preview",
            definition,
            &definitions,
        )
        .context("composition_graph")?;
        let has_plugins = ["sources", "pipeline"].iter().any(|key| {
            compiled
                .definition
                .get(key)
                .and_then(Value::as_array)
                .is_some_and(|items| {
                    items.iter().any(|item| {
                        item.get("capability_id")
                            .is_some_and(|id| !id.is_null())
                    })
                })
        });
        if has_plugins {
            self.runtime_catalog
                .as_ref()
                .context("Pipeline plugin validation is unavailable")?
                .validate_pipeline_capabilities(&compiled.definition)
                .await
                .context("plugin_capabilities")?;
        }
        Ok(compiled)
    }

    /// Creates a pipeline service from reusable domain ports.
    pub fn new(
        store: Arc<dyn PipelineStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
    ) -> Self {
        Self::with_catalog(store, identity, auth, audit, None)
    }

    /// Keeps plugin metadata supplied by trusted composition rather than
    /// pipeline JSON.
    pub fn with_catalog(
        store: Arc<dyn PipelineStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
        catalog: Option<Arc<CapabilityCatalog>>,
    ) -> Self {
        Self {
            store,
            identity,
            auth,
            audit,
            catalog,
            runtime_catalog: None,
        }
    }

    /// Keeps executable discovery owned by the deployed plugin runtime.
    pub fn with_runtime_catalog(
        mut self,
        runtime: Arc<dyn ConversationAgent>,
    ) -> Self {
        self.runtime_catalog = Some(runtime);
        self
    }

    /// Active membership is required even though discovery grants no execution
    /// rights.
    #[tracing::instrument(skip_all, name = "pipeline.capabilities")]
    pub async fn capabilities(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<CapabilityCatalog, DiscoveryError> {
        self.identity
            .verify_user(tenant_id, user_id)
            .await
            .map_err(|_| DiscoveryError::Forbidden)?;
        if let Some(runtime) = &self.runtime_catalog {
            return runtime
                .pipeline_capabilities()
                .await
                .map_err(|_| DiscoveryError::Unavailable);
        }
        match self.catalog.as_deref() {
            Some(catalog) => Ok(catalog.clone()),
            None => builtin_catalog()
                .cloned()
                .map_err(|_| DiscoveryError::Unavailable),
        }
    }

    /// Starts audit before applying identity, SpiceDB, and Cedar checks.
    #[expect(
        clippy::too_many_arguments,
        reason = "authorization requires explicit actor, target, and revision context"
    )]
    async fn begin_authorized(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        action: AuditAction,
        pipeline_id: &str,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
        revision_id: Option<&str>,
    ) -> Result<AuditOperation> {
        let mut target = AuditTarget::new(action, "pipeline", pipeline_id);
        if let Some(revision_id) = revision_id {
            target = target.with_revision_id(revision_id);
        }
        let operation = self
            .audit
            .begin(tenant_id, user_id, target, context)
            .await?;
        if let Err(error) = self.identity.verify_user(tenant_id, user_id).await
        {
            operation.denied("identity_denied").await?;
            return Err(error);
        }
        match self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                permission,
                resource_type,
                resource_id,
                Some(context),
            )
            .await
        {
            Ok(true) => Ok(operation),
            Ok(false) => {
                operation.denied("authorization_denied").await?;
                Err(PipelineAccessDenied.into())
            },
            Err(error) => {
                operation.failed("authorization_dependency_failed").await?;
                Err(error)
            },
        }
    }

    /// Rejects identifiers that cannot be safely shared by GraphQL, SpiceDB,
    /// and S3.
    fn validate_pipeline_id(pipeline_id: &str) -> Result<&str> {
        let pipeline_id = pipeline_id.trim();
        if pipeline_id.is_empty() ||
            pipeline_id.len() > MAX_PIPELINE_ID_BYTES ||
            !pipeline_id.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
            })
        {
            bail!("Invalid pipeline identifier");
        }
        Ok(pipeline_id)
    }

    /// Validates the canonical pipeline name without introducing an alias.
    fn validate_name(name: &str) -> Result<&str> {
        let name = name.trim();
        if name.is_empty() || name.len() > MAX_NAME_BYTES {
            bail!("Pipeline name must contain 1 to {MAX_NAME_BYTES} bytes");
        }
        Ok(name)
    }

    /// Validates an immutable revision message used for provenance.
    fn validate_message(message: &str) -> Result<&str> {
        let message = message.trim();
        if message.is_empty() || message.len() > MAX_MESSAGE_BYTES {
            bail!(
                "Pipeline revision message must contain 1 to {MAX_MESSAGE_BYTES} bytes"
            );
        }
        Ok(message)
    }

    /// Validates the graph constraints implemented by `platform/pipeline`.
    fn validate_definition(
        definition: &Value,
        expected_name: &str,
    ) -> Result<()> {
        if let Some(object) = definition.as_object() {
            for key in object.keys() {
                if !matches!(
                    key.as_str(),
                    "name" | "version" | "sources" | "pipeline" | "editor"
                ) {
                    bail!(
                        "Pipeline definitions cannot contain service setting '{key}'"
                    );
                }
            }
        }
        let object = definition
            .as_object()
            .context("Pipeline definition must be a JSON object")?;
        let name = object
            .get("name")
            .and_then(Value::as_str)
            .context("Pipeline definition requires name")?;
        if name != expected_name {
            bail!("Pipeline definition name must match the pipeline name");
        }
        let sources = object
            .get("sources")
            .and_then(Value::as_array)
            .context("Pipeline definition requires a sources array")?;
        let steps = object
            .get("pipeline")
            .and_then(Value::as_array)
            .context("Pipeline definition requires a pipeline array")?;

        let mut known = HashSet::with_capacity(sources.len() + steps.len());
        for source in sources {
            let source_id = source
                .get("id")
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty())
                .context("Every pipeline source requires an id")?;
            if !known.insert(source_id) {
                bail!("Duplicate pipeline node identifier: {source_id}");
            }
        }
        let mut dependencies = HashMap::with_capacity(steps.len());
        for step in steps {
            let step_id = step
                .get("step")
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty())
                .context("Every pipeline step requires a step identifier")?;
            if !known.insert(step_id) {
                bail!("Duplicate pipeline node identifier: {step_id}");
            }
            let input_from = step
                .get("input_from")
                .and_then(Value::as_array)
                .context("Every pipeline step requires an input_from array")?;
            let values = input_from
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .context("Pipeline dependencies must be strings")
                })
                .collect::<Result<Vec<_>>>()?;
            dependencies.insert(step_id, values);
        }
        for (step, inputs) in &dependencies {
            for input in inputs {
                if !known.contains(input) {
                    bail!(
                        "Pipeline step '{step}' references unknown dependency '{input}'"
                    );
                }
            }
        }

        let mut in_degree = HashMap::with_capacity(known.len());
        let mut downstream: HashMap<&str, Vec<&str>> = HashMap::new();
        for node in &known {
            in_degree.insert(*node, 0_usize);
        }
        for (step, inputs) in &dependencies {
            for input in inputs {
                downstream.entry(input).or_default().push(step);
                let degree = in_degree
                    .get_mut(step)
                    .context("Pipeline step disappeared during validation")?;
                *degree += 1;
            }
        }
        let mut ready = in_degree
            .iter()
            .filter_map(|(node, degree)| (*degree == 0).then_some(*node))
            .collect::<VecDeque<_>>();
        let mut visited = 0_usize;
        while let Some(node) = ready.pop_front() {
            visited += 1;
            if let Some(children) = downstream.get(node) {
                for child in children {
                    let degree = in_degree.get_mut(child).context(
                        "Pipeline child disappeared during validation",
                    )?;
                    *degree = degree.saturating_sub(1);
                    if *degree == 0 {
                        ready.push_back(child);
                    }
                }
            }
        }
        if visited != known.len() {
            bail!("Pipeline graph contains a cycle");
        }
        Ok(())
    }

    /// Creates a pipeline and root revision with owner relationships.
    #[expect(
        clippy::too_many_arguments,
        reason = "pipeline creation requires explicit immutable revision provenance"
    )]
    pub async fn create(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        name: &str,
        definition: &Value,
        message: &str,
    ) -> Result<PipelineDefinition> {
        let pipeline_id = Self::validate_pipeline_id(pipeline_id)?;
        let name = Self::validate_name(name)?;
        let message = Self::validate_message(message)?;
        Self::validate_definition(definition, name)?;
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditAction::CreatePipeline,
                pipeline_id,
                Permission::CreatePipeline,
                "tenant",
                tenant_id,
                None,
            )
            .await?;
        if let Err(error) = self
            .compose(
                tenant_id,
                user_id,
                context,
                pipeline_id,
                definition,
                false,
            )
            .await
        {
            operation.failed("composition_rejected").await?;
            return Err(error);
        }
        let created = match self
            .store
            .create(
                tenant_id,
                &NewPipelineRevision {
                    pipeline_id,
                    name,
                    owner_id: user_id,
                    definition,
                    author_id: user_id,
                    message,
                },
            )
            .await
        {
            Ok(created) => created,
            Err(error) => {
                operation.failed("registry_mutation_failed").await?;
                return Err(error);
            },
        };
        let resource_id = format!("{tenant_id}/{pipeline_id}");
        for (relation, subject_type, subject_id) in
            [("parent", "tenant", tenant_id), ("owner", "user", user_id)]
        {
            if let Err(error) = self
                .auth
                .upsert_relationship(
                    "pipeline",
                    &resource_id,
                    relation,
                    subject_type,
                    subject_id,
                )
                .await
            {
                operation.failed("authorization_replication_failed").await?;
                return Err(error);
            }
        }
        operation
            .with_revision_id(&created.head_revision_id)
            .succeeded()
            .await?;
        Ok(created)
    }

    /// Appends a pipeline revision under optimistic head concurrency.
    #[expect(
        clippy::too_many_arguments,
        reason = "pipeline revision provenance is intentionally explicit"
    )]
    pub async fn update(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        expected_head_revision_id: &str,
        name: &str,
        definition: &Value,
        message: &str,
    ) -> Result<PipelineDefinition> {
        let pipeline_id = Self::validate_pipeline_id(pipeline_id)?;
        let name = Self::validate_name(name)?;
        let message = Self::validate_message(message)?;
        Self::validate_definition(definition, name)?;
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditAction::UpdatePipeline,
                pipeline_id,
                Permission::Edit,
                "pipeline",
                pipeline_id,
                None,
            )
            .await?;
        if let Err(error) = self
            .compose(
                tenant_id,
                user_id,
                context,
                pipeline_id,
                definition,
                false,
            )
            .await
        {
            operation.failed("composition_rejected").await?;
            return Err(error);
        }
        match self
            .store
            .update(
                tenant_id,
                expected_head_revision_id,
                &NewPipelineRevision {
                    pipeline_id,
                    name,
                    owner_id: user_id,
                    definition,
                    author_id: user_id,
                    message,
                },
            )
            .await
        {
            Ok(updated) => {
                operation
                    .with_revision_id(&updated.head_revision_id)
                    .succeeded()
                    .await?;
                Ok(updated)
            },
            Err(error) => {
                operation.failed("registry_mutation_failed").await?;
                Err(error)
            },
        }
    }

    /// Lists only pipeline definitions visible to the principal.
    pub async fn list(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<PipelineDefinition>> {
        self.identity.verify_user(tenant_id, user_id).await?;
        let candidates =
            self.store.list(tenant_id, limit.clamp(1, 100)).await?;
        let mut visible = Vec::with_capacity(candidates.len());
        for pipeline in candidates {
            if self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "pipeline",
                    &pipeline.pipeline_id,
                    Some(context),
                )
                .await?
            {
                visible.push(pipeline);
            }
        }
        Ok(visible)
    }

    /// Publishes only the current immutable pipeline head for runtime
    /// discovery.
    pub async fn publish(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<PipelineDefinition> {
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditAction::PublishPipeline,
                pipeline_id,
                Permission::Publish,
                "pipeline",
                pipeline_id,
                Some(revision_id),
            )
            .await?;
        let pipeline = self
            .store
            .get(tenant_id, pipeline_id)
            .await?
            .context("Pipeline is unavailable")?;
        if pipeline.head_revision_id != revision_id {
            operation.failed("stale_revision").await?;
            bail!("Only the current pipeline head can be published");
        }
        if let Err(error) = self
            .compose(
                tenant_id,
                user_id,
                context,
                pipeline_id,
                &pipeline.definition,
                true,
            )
            .await
        {
            operation.failed("composition_rejected").await?;
            return Err(error);
        }
        match self
            .store
            .publish(tenant_id, pipeline_id, revision_id)
            .await
        {
            Ok(published) => {
                operation.succeeded().await?;
                Ok(published)
            },
            Err(error) => {
                operation.failed("registry_mutation_failed").await?;
                Err(error)
            },
        }
    }

    /// Soft-deletes the definition and atomically retires runtime discovery.
    pub async fn delete(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        pipeline_id: &str,
        expected_head_revision_id: &str,
    ) -> Result<()> {
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditAction::DeletePipeline,
                pipeline_id,
                Permission::Delete,
                "pipeline",
                pipeline_id,
                Some(expected_head_revision_id),
            )
            .await?;
        if let Err(error) = self
            .store
            .delete(tenant_id, pipeline_id, expected_head_revision_id)
            .await
        {
            operation.failed("registry_mutation_failed").await?;
            return Err(error);
        }
        operation.succeeded().await
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use anyhow::{Result, anyhow, ensure};

    use super::*;
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization, audit, identity,
    };

    #[derive(Default)]
    struct MemoryPipelineStore {
        pipeline: Mutex<Option<PipelineDefinition>>,
        dependencies:
            Mutex<std::collections::BTreeMap<String, PipelineDefinition>>,
    }

    impl MemoryPipelineStore {
        /// Maps one revision input into the current in-memory projection.
        fn projection(
            revision: &NewPipelineRevision<'_>,
        ) -> PipelineDefinition {
            PipelineDefinition {
                authoring: Default::default(),
                previous_revision_id: None,
                pipeline_id: revision.pipeline_id.to_owned(),
                name: revision.name.to_owned(),
                owner_id: revision.owner_id.to_owned(),
                head_revision_id: uuid::Uuid::new_v4().simple().to_string(),
                published_revision_id: None,
                definition: revision.definition.clone(),
                author_id: revision.author_id.to_owned(),
                message: revision.message.to_owned(),
                created_at_ms: 1,
                updated_at_ms: 1,
                deleted_at_ms: None,
            }
        }

        /// Locks the test projection without hiding poisoning failures.
        fn lock(
            &self,
        ) -> Result<std::sync::MutexGuard<'_, Option<PipelineDefinition>>>
        {
            self.pipeline.lock().map_err(|error| {
                anyhow!("pipeline test lock poisoned: {error}")
            })
        }
    }

    #[async_trait::async_trait]
    impl PipelineStore for MemoryPipelineStore {
        async fn author(
            &self,
            tenant: &str,
            expected: Option<&str>,
            revision: &NewPipelineRevision<'_>,
            command: &galadril_registry::authoring::AuthoringCommand,
        ) -> Result<PipelineDefinition> {
            ensure!(matches!(command, galadril_registry::authoring::AuthoringCommand::SaveDraft { .. }), "test command unsupported");
            match expected {
                Some(expected) => {
                    self.update(tenant, expected, revision).await
                },
                None => self.create(tenant, revision).await,
            }
        }

        async fn create(
            &self,
            _tenant_id: &str,
            revision: &NewPipelineRevision<'_>,
        ) -> Result<PipelineDefinition> {
            let mut current = self.lock()?;
            ensure!(current.is_none(), "pipeline already exists");
            let projection = Self::projection(revision);
            *current = Some(projection.clone());
            Ok(projection)
        }

        async fn update(
            &self,
            _tenant_id: &str,
            expected_head_revision_id: &str,
            revision: &NewPipelineRevision<'_>,
        ) -> Result<PipelineDefinition> {
            let mut current = self.lock()?;
            let existing = current.as_ref().context("pipeline is missing")?;
            ensure!(
                existing.head_revision_id == expected_head_revision_id,
                "head changed"
            );
            let mut projection = Self::projection(revision);
            projection.owner_id = existing.owner_id.clone();
            projection.created_at_ms = existing.created_at_ms;
            *current = Some(projection.clone());
            Ok(projection)
        }

        async fn list(
            &self,
            _tenant_id: &str,
            _limit: usize,
        ) -> Result<Vec<PipelineDefinition>> {
            let mut result = self
                .dependencies
                .lock()
                .map_err(|_| anyhow!("dependency lock"))?
                .values()
                .cloned()
                .collect::<Vec<_>>();
            result.extend(self.lock()?.iter().cloned());
            Ok(result)
        }

        async fn get(
            &self,
            _tenant_id: &str,
            pipeline_id: &str,
        ) -> Result<Option<PipelineDefinition>> {
            if let Some(pipeline) = self
                .dependencies
                .lock()
                .map_err(|_| anyhow!("dependency lock"))?
                .get(pipeline_id)
            {
                return Ok(Some(pipeline.clone()));
            }
            Ok(self
                .lock()?
                .as_ref()
                .filter(|pipeline| pipeline.pipeline_id == pipeline_id)
                .cloned())
        }

        async fn get_revision(
            &self,
            tenant_id: &str,
            pipeline_id: &str,
            revision_id: &str,
        ) -> Result<Option<PipelineDefinition>> {
            Ok(self
                .get(tenant_id, pipeline_id)
                .await?
                .filter(|pipeline| pipeline.head_revision_id == revision_id))
        }

        async fn publish(
            &self,
            _tenant_id: &str,
            _pipeline_id: &str,
            revision_id: &str,
        ) -> Result<PipelineDefinition> {
            let mut current = self.lock()?;
            let pipeline = current.as_mut().context("pipeline is missing")?;
            ensure!(pipeline.head_revision_id == revision_id, "stale head");
            pipeline.published_revision_id = Some(revision_id.to_owned());
            Ok(pipeline.clone())
        }

        async fn delete(
            &self,
            _tenant_id: &str,
            _pipeline_id: &str,
            expected_head_revision_id: &str,
        ) -> Result<()> {
            let mut current = self.lock()?;
            let pipeline = current.as_mut().context("pipeline is missing")?;
            ensure!(
                pipeline.head_revision_id == expected_head_revision_id,
                "stale head"
            );
            pipeline.deleted_at_ms = Some(2);
            Ok(())
        }
    }

    fn pipeline(inputs: &[&str]) -> Value {
        serde_json::json!({
            "name": "example",
            "sources": [{
                "id": "source",
                "topic": "raw",
                "match_pattern": "^input/",
                "schema_path": "schemas/input.avsc"
            }],
            "pipeline": [{
                "step": "sink",
                "type": "sink",
                "input_from": inputs,
            }]
        })
    }

    #[tokio::test]
    async fn draft_authoring_does_not_require_an_executable_graph()
    -> Result<()> {
        use galadril_registry::authoring::AuthoringCommand;
        let store = Arc::new(MemoryPipelineStore::default());
        let (audit, _) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit,
        );
        let definition = serde_json::json!({"name":"Unfinished","sources":[],"pipeline":[{"step":"reply","type":"sink","input_from":[]}]});
        let value = service
            .author(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "draft",
                None,
                "Unfinished",
                &definition,
                "Draft",
                AuthoringCommand::SaveDraft {
                    branch: "main".to_owned(),
                },
            )
            .await?;
        ensure!(value.definition == definition);
        ensure!(value.published_revision_id.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn authoring_denial_precedes_any_storage_read_or_write() -> Result<()>
    {
        use galadril_registry::authoring::AuthoringCommand;
        let store = Arc::new(MemoryPipelineStore::default());
        let (audit, _) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            audit,
        );
        let result = service
            .author(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "hidden",
                Some("current"),
                "Hidden",
                &serde_json::json!({}),
                "Draft",
                AuthoringCommand::SaveDraft {
                    branch: "main".to_owned(),
                },
            )
            .await;
        ensure!(result.is_err());
        ensure!(store.lock()?.is_none());
        Ok(())
    }

    #[test]
    fn tenant_pipeline_cannot_replace_service_credentials() {
        let mut definition = pipeline(&["source"]);
        if let Some(object) = definition.as_object_mut() {
            object.insert(
                "connectors".to_owned(),
                serde_json::json!({"postgres": {"user": "admin"}}),
            );
        }
        assert!(
            PipelineService::validate_definition(&definition, "example")
                .is_err()
        );
    }

    #[test]
    fn canonical_pipeline_shape_and_dependencies_are_validated() {
        assert!(
            PipelineService::validate_definition(
                &pipeline(&["source"]),
                "example"
            )
            .is_ok()
        );
        assert!(
            PipelineService::validate_definition(
                &pipeline(&["missing"]),
                "example"
            )
            .is_err()
        );
        assert!(
            PipelineService::validate_definition(
                &pipeline(&["source"]),
                "other"
            )
            .is_err()
        );
    }

    #[test]
    fn pipeline_identifiers_reject_cross_tenant_path_segments() {
        assert!(PipelineService::validate_pipeline_id("daily-import").is_ok());
        assert!(
            PipelineService::validate_pipeline_id("tenant/other").is_err()
        );
    }

    #[tokio::test]
    async fn capabilities_require_active_membership_and_use_trusted_composition()
    -> Result<()> {
        let metadata = serde_json::json!({"schema_version":1,"capabilities":[{"id":"law.algorithm.timeline","plugin_id":"law","plugin_version":"1.0.0","kind":"algorithm","targets":["pipeline","scribe"],"name":"Timeline","description":"Analyze chronology","parameters_schema":{"type":"object"}}]});
        let catalog = Arc::new(CapabilityCatalog::parse(
            &serde_json::to_vec(&metadata)?,
        )?);
        for active in [false, true] {
            let (audit, _) = audit();
            let service = PipelineService::with_catalog(
                Arc::new(MemoryPipelineStore::default()),
                identity(active),
                TestAuthorization::new(AuthorizationDecision::Deny),
                audit,
                Some(catalog.clone()),
            );
            let result = service.capabilities("tenant_a", "user_a").await;
            if active {
                ensure!(
                    result?.capabilities.first().is_some_and(
                        |item| item.id == "law.algorithm.timeline"
                    )
                );
            } else {
                ensure!(matches!(result, Err(DiscoveryError::Forbidden)));
            }
        }
        Ok(())
    }

    #[tokio::test]
    async fn authorized_transfer_uses_the_pin_and_rechecks_target_publication_rights()
    -> Result<()> {
        let store = Arc::new(MemoryPipelineStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, _) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            authorization.clone(),
            audit,
        );
        let child_definition = pipeline(&["source"]);
        let mut child =
            MemoryPipelineStore::projection(&NewPipelineRevision {
                pipeline_id: "child",
                name: "example",
                owner_id: "owner",
                definition: &child_definition,
                author_id: "user_a",
                message: "child",
            });
        child.published_revision_id = Some(child.head_revision_id.clone());
        store
            .dependencies
            .lock()
            .map_err(|_| anyhow!("dependency lock"))?
            .insert("child".to_owned(), child.clone());
        let definition = serde_json::json!({"name":"example","sources":[{"id":"source"}],"pipeline":[{"step":"send","type":"pipeline","input_from":["source"],"pipeline_ref":{"pipeline_id":"child","revision_id":child.head_revision_id,"input_source":"source"}}]});
        let root = service
            .create(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "root",
                "example",
                &definition,
                "linked",
            )
            .await?;
        let preview = service
            .compose(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "root",
                &definition,
                false,
            )
            .await?;
        ensure!(preview.blocks == 4 && preview.transfers == 1);
        ensure!(
            preview.definition.pointer("/pipeline/1/origin/pipeline_id") ==
                Some(&serde_json::json!("child"))
        );
        service
            .publish(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "root",
                &root.head_revision_id,
            )
            .await?;
        ensure!(
            authorization
                .pipeline_checks
                .lock()
                .map_err(|_| anyhow!("authorization lock"))?
                .contains(&(Permission::Publish, "child".to_owned()))
        );
        Ok(())
    }

    #[tokio::test]
    async fn a_transfer_cannot_read_a_hidden_target_or_publish_after_revocation()
    -> Result<()> {
        let store = Arc::new(MemoryPipelineStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, _) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            authorization.clone(),
            audit,
        );
        let context = QueryContext::default();
        let mut definition = pipeline(&["source"]);
        if let Some(steps) =
            definition.get_mut("pipeline").and_then(Value::as_array_mut)
        {
            steps.clear();
            steps.push(serde_json::json!({"step":"send","type":"pipeline","input_from":["source"],"pipeline_ref":{"pipeline_id":"hidden","revision_id":"a".repeat(32),"input_source":"source"}}));
        }
        *authorization
            .denied_pipeline
            .lock()
            .map_err(|_| anyhow!("test lock poisoned"))? =
            Some("hidden".to_owned());
        ensure!(
            service
                .create(
                    "tenant_a",
                    "user_a",
                    &context,
                    "root",
                    "example",
                    &definition,
                    "link"
                )
                .await
                .is_err()
        );
        ensure!(store.lock()?.is_none());
        let revision = NewPipelineRevision {
            pipeline_id: "root",
            name: "example",
            owner_id: "user_a",
            definition: &definition,
            author_id: "user_a",
            message: "previously authorized",
        };
        let root = store.create("tenant_a", &revision).await?;
        ensure!(
            service
                .publish(
                    "tenant_a",
                    "user_a",
                    &context,
                    "root",
                    &root.head_revision_id
                )
                .await
                .is_err()
        );
        ensure!(
            store
                .lock()?
                .as_ref()
                .is_some_and(|item| item.published_revision_id.is_none())
        );
        ensure!(
            authorization
                .pipeline_checks
                .lock()
                .map_err(|_| anyhow!("test lock poisoned"))?
                .iter()
                .any(|(_, id)| id == "hidden")
        );
        Ok(())
    }

    #[tokio::test]
    async fn reading_a_pipeline_requires_permission_before_storage_lookup()
    -> Result<()> {
        let (audit, _) = audit();
        let service = PipelineService::new(
            Arc::new(MemoryPipelineStore::default()),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            audit,
        );
        ensure!(
            service
                .read(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    "secret",
                    None
                )
                .await
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn authorized_pipeline_lifecycle_preserves_revisions_and_audit()
    -> Result<()> {
        let store = Arc::new(MemoryPipelineStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, audit_store) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            authorization.clone(),
            audit,
        );
        let context = QueryContext {
            request_id: "request-pipeline".to_owned(),
            ..QueryContext::default()
        };

        let created = service
            .create(
                "tenant_a",
                "user_a",
                &context,
                "daily",
                "example",
                &pipeline(&["source"]),
                "root",
            )
            .await?;
        let updated = service
            .update(
                "tenant_a",
                "user_a",
                &context,
                "daily",
                &created.head_revision_id,
                "example",
                &pipeline(&["source"]),
                "change",
            )
            .await?;
        ensure!(updated.head_revision_id != created.head_revision_id);
        ensure!(
            service
                .list("tenant_a", "user_a", &context, 10)
                .await?
                .len() ==
                1
        );
        let published = service
            .publish(
                "tenant_a",
                "user_a",
                &context,
                "daily",
                &updated.head_revision_id,
            )
            .await?;
        ensure!(
            published.published_revision_id.as_deref() ==
                Some(updated.head_revision_id.as_str())
        );
        service
            .delete(
                "tenant_a",
                "user_a",
                &context,
                "daily",
                &updated.head_revision_id,
            )
            .await?;

        ensure!(
            authorization
                .mutations
                .lock()
                .map_err(|error| anyhow!(
                    "authorization test lock poisoned: {error}"
                ))?
                .len() ==
                2
        );
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 8);
        ensure!(
            events
                .iter()
                .all(|event| event.request_id == "request-pipeline")
        );
        ensure!(
            events
                .iter()
                .filter(|event| event.revision_id.is_some())
                .count() ==
                6
        );
        Ok(())
    }

    #[tokio::test]
    async fn pipeline_creation_denial_is_audited_without_persistence()
    -> Result<()> {
        let store = Arc::new(MemoryPipelineStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Deny);
        let (audit, audit_store) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            authorization,
            audit,
        );
        let result = service
            .create(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "daily",
                "example",
                &pipeline(&["source"]),
                "root",
            )
            .await;
        ensure!(result.is_err());
        ensure!(store.lock()?.is_none());
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 2);
        ensure!(events.get(1).map(|event| event.outcome) == Some(crate::application::ports::audit_store::AuditOutcome::Denied));
        Ok(())
    }

    #[tokio::test]
    async fn authorization_dependency_failure_is_audited_and_fail_closed()
    -> Result<()> {
        let store = Arc::new(MemoryPipelineStore::default());
        let (audit, audit_store) = audit();
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Fail),
            audit,
        );
        let result = service
            .create(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "daily",
                "example",
                &pipeline(&["source"]),
                "root",
            )
            .await;
        ensure!(result.is_err());
        ensure!(store.lock()?.is_none());
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 2);
        ensure!(
            events
                .get(1)
                .and_then(|event| event.failure_kind.as_deref()) ==
                Some("authorization_dependency_failed")
        );
        Ok(())
    }

    #[tokio::test]
    async fn plugin_validation_precedes_storage_and_requires_a_live_runtime()
    -> Result<()> {
        use std::sync::atomic::{AtomicUsize, Ordering};

        use crate::application::ports::conversation_agent::{
            AgentModelCatalog, AgentRequest, AgentStream, ConversationAgent,
        };
        struct Validator(AtomicUsize);
        #[async_trait::async_trait]
        impl ConversationAgent for Validator {
            async fn models(&self) -> Result<AgentModelCatalog> {
                Ok(AgentModelCatalog::default())
            }

            async fn start(
                &self,
                _request: AgentRequest<'_>,
            ) -> Result<AgentStream> {
                bail!("unused")
            }

            async fn validate_pipeline_capabilities(
                &self,
                definition: &Value,
            ) -> Result<()> {
                self.0.fetch_add(1, Ordering::SeqCst);
                ensure!(
                    definition
                        .pointer("/pipeline/0/capability_version")
                        .and_then(Value::as_str) ==
                        Some("1.0.0"),
                    "Plugin version unavailable"
                );
                Ok(())
            }
        }
        let store = Arc::new(MemoryPipelineStore::default());
        let (audit_service, _) = audit();
        let validator = Arc::new(Validator(AtomicUsize::new(0)));
        let service = PipelineService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit_service,
        )
        .with_runtime_catalog(validator.clone());
        let definition = serde_json::json!({"name":"example", "sources":[{"id":"source"}], "pipeline":[{"step":"custom","type":"algorithm","input_from":["source"],"capability_id":"fixture.algorithm.count","capability_version":"2.0.0"}]});
        ensure!(
            service
                .create(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    "root",
                    "example",
                    &definition,
                    "plugin"
                )
                .await
                .is_err()
        );
        ensure!(store.lock()?.is_none());
        assert_eq!(validator.0.load(Ordering::SeqCst), 1);
        let (audit_service, _) = audit();
        let offline = PipelineService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            audit_service,
        );
        ensure!(
            offline
                .compose(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    "root",
                    &definition,
                    false
                )
                .await
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn runtime_catalog_is_requested_only_for_active_members()
    -> Result<()> {
        use std::sync::atomic::{AtomicUsize, Ordering};

        use crate::application::ports::conversation_agent::{
            AgentModelCatalog, AgentRequest, AgentStream, ConversationAgent,
        };
        struct RuntimeCatalog(AtomicUsize);
        #[async_trait::async_trait]
        impl ConversationAgent for RuntimeCatalog {
            async fn models(&self) -> Result<AgentModelCatalog> {
                Ok(AgentModelCatalog::default())
            }

            async fn start(
                &self,
                _request: AgentRequest<'_>,
            ) -> Result<AgentStream> {
                bail!("unused")
            }

            async fn pipeline_capabilities(
                &self,
            ) -> Result<CapabilityCatalog> {
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(CapabilityCatalog {
                    schema_version: 1,
                    capabilities: Vec::new(),
                })
            }
        }
        let runtime = Arc::new(RuntimeCatalog(AtomicUsize::new(0)));
        for active in [false, true] {
            let (audit, _) = audit();
            let service = PipelineService::new(
                Arc::new(MemoryPipelineStore::default()),
                identity(active),
                TestAuthorization::new(AuthorizationDecision::Deny),
                audit,
            )
            .with_runtime_catalog(runtime.clone());
            assert_eq!(
                service.capabilities("tenant_a", "user_a").await.is_ok(),
                active
            );
            assert_eq!(runtime.0.load(Ordering::SeqCst), usize::from(active));
        }
        Ok(())
    }
}
