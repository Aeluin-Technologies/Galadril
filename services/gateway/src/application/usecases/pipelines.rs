//! Authorized, versioned pipeline authoring and runtime publication.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use serde_json::Value;

use crate::application::ports::pipeline_store::{
    NewPipelineRevision, PipelineDefinition, PipelineStore,
};
use crate::application::usecases::audit::{
    AuditAction, AuditOperation, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;

const MAX_PIPELINE_ID_BYTES: usize = 128;
const MAX_NAME_BYTES: usize = 256;
const MAX_MESSAGE_BYTES: usize = 1024;

/// Coordinates pipeline history, authorization, publication, and audit.
pub struct PipelineService {
    store: Arc<dyn PipelineStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
}

impl PipelineService {
    /// Creates a pipeline service from reusable domain ports.
    pub fn new(
        store: Arc<dyn PipelineStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
    ) -> Self {
        Self {
            store,
            identity,
            auth,
            audit,
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
                bail!("Authorization denied");
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
                    "name" | "version" | "sources" | "pipeline"
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
        let created = match self
            .store
            .create(
                tenant_id,
                &NewPipelineRevision {
                    pipeline_id,
                    revision_id: "",
                    parent_revision_id: None,
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
                operation.failed("database_mutation_failed").await?;
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
        match self
            .store
            .update(
                tenant_id,
                expected_head_revision_id,
                &NewPipelineRevision {
                    pipeline_id,
                    revision_id: "",
                    parent_revision_id: Some(expected_head_revision_id),
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
                operation.failed("database_mutation_failed").await?;
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
                operation.failed("database_mutation_failed").await?;
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
            operation.failed("database_mutation_failed").await?;
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
    }

    impl MemoryPipelineStore {
        /// Maps one revision input into the current in-memory projection.
        fn projection(
            revision: &NewPipelineRevision<'_>,
        ) -> PipelineDefinition {
            PipelineDefinition {
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
            Ok(self.lock()?.iter().cloned().collect())
        }

        async fn get(
            &self,
            _tenant_id: &str,
            _pipeline_id: &str,
        ) -> Result<Option<PipelineDefinition>> {
            Ok(self.lock()?.clone())
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
}
