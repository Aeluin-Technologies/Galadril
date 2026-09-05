//! Authorized control-plane reads over canonical platform records.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result, bail};

use crate::application::ports::audit_store::{AuditEvent, AuditFilter};
use crate::application::ports::control_plane_store::{
    ControlPlaneStore, OntologyCatalogEntry, OntologyPublication,
    PipelineExecution, PipelineOntologyBinding,
};
use crate::application::ports::iam_store::{
    IamRole, IamStore, IamUser, RoleAssignment,
};
use crate::application::usecases::audit::{
    AuditAction, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;

const HARD_LIMIT: usize = 100;

/// Coordinates authorized reads and ontology production lifecycle operations.
pub struct ControlPlaneService {
    store: Arc<dyn ControlPlaneStore>,
    iam: Arc<dyn IamStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
}

impl ControlPlaneService {
    /// Creates the control-plane service from authoritative domain ports.
    pub fn new(
        store: Arc<dyn ControlPlaneStore>,
        iam: Arc<dyn IamStore>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
    ) -> Self {
        Self {
            store,
            iam,
            identity,
            auth,
            audit,
        }
    }

    /// Validates the ontology runtime identifier grammar from
    /// `platform/ontology`.
    fn validate_ontology_id(ontology_id: &str) -> Result<&str> {
        let ontology_id = ontology_id.trim();
        if ontology_id.is_empty() ||
            ontology_id.len() > 128 ||
            !ontology_id.bytes().enumerate().all(|(index, byte)| {
                byte.is_ascii_alphanumeric() ||
                    (index > 0 && matches!(byte, b'_' | b'.' | b':' | b'-'))
            })
        {
            bail!("Ontology identifier contains unsupported characters");
        }
        Ok(ontology_id)
    }

    /// Accepts native TerminusDB commit identifiers and legacy imported IDs.
    fn validate_ontology_revision_id(revision_id: &str) -> Result<&str> {
        if !(20..=128).contains(&revision_id.len()) ||
            !galadril_versioning::valid_segment(revision_id) ||
            revision_id.bytes().any(|byte| byte.is_ascii_uppercase())
        {
            bail!(
                "Ontology revision identifier must be a native commit identifier"
            );
        }
        Ok(revision_id)
    }

    /// Validates the lowercase UUID form used for ontology publications.
    fn validate_ontology_publication_id(publication_id: &str) -> Result<&str> {
        if publication_id.len() != 32 ||
            !publication_id.bytes().all(|byte| {
                byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')
            })
        {
            bail!(
                "Ontology publication identifier must be 32 lowercase hexadecimal characters"
            );
        }
        Ok(publication_id)
    }

    /// Publishes an authoritative validated materialization as production.
    #[expect(
        clippy::too_many_arguments,
        reason = "publication identity and provenance are intentionally explicit"
    )]
    pub async fn publish_ontology(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        ontology_id: &str,
        display_name: &str,
        revision_id: &str,
        metadata: &serde_json::Value,
    ) -> Result<OntologyPublication> {
        self.verify_user(tenant_id, user_id).await?;
        let ontology_id = Self::validate_ontology_id(ontology_id)?;
        let display_name = display_name.trim();
        if display_name.is_empty() || display_name.len() > 256 {
            bail!("Ontology display name must contain 1 to 256 bytes");
        }
        if !metadata.is_object() {
            bail!("Ontology publication metadata must be a JSON object");
        }
        let revision_id = Self::validate_ontology_revision_id(revision_id)?;
        let create_only =
            !self.store.ontology_exists(tenant_id, ontology_id).await?;
        let (permission, resource_type, resource_id) = if create_only {
            (Permission::CreateOntology, "tenant", tenant_id)
        } else {
            (Permission::Publish, "ontology", ontology_id)
        };
        let publication_id = uuid::Uuid::new_v4().simple().to_string();
        let operation = self
            .audit
            .begin(
                tenant_id,
                user_id,
                AuditTarget::new(
                    AuditAction::PublishOntology,
                    "ontology",
                    ontology_id,
                )
                .with_revision_id(revision_id)
                .with_publication_id(&publication_id),
                context,
            )
            .await?;
        let authorized = match self
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
            Ok(decision) => decision,
            Err(error) => {
                operation.failed("authorization_dependency_failed").await?;
                return Err(error);
            },
        };
        if !authorized {
            operation.denied("authorization_denied").await?;
            bail!("Authorization denied");
        }
        let publication = match self
            .store
            .publish_ontology(
                tenant_id,
                ontology_id,
                display_name,
                &publication_id,
                revision_id,
                metadata,
                create_only,
            )
            .await
        {
            Ok(publication) => publication,
            Err(error) => {
                operation.failed("database_mutation_failed").await?;
                return Err(error);
            },
        };
        if create_only {
            let resource_id = format!("{tenant_id}/{ontology_id}");
            for (relation, subject_type, subject_id) in
                [("parent", "tenant", tenant_id), ("owner", "user", user_id)]
            {
                if let Err(error) = self
                    .auth
                    .upsert_relationship(
                        "ontology",
                        &resource_id,
                        relation,
                        subject_type,
                        subject_id,
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
        operation.succeeded().await?;
        Ok(publication)
    }

    /// Retires production ontology access without deleting immutable
    /// revisions.
    pub async fn retire_ontology(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        ontology_id: &str,
        publication_id: &str,
        revision_id: &str,
    ) -> Result<()> {
        self.verify_user(tenant_id, user_id).await?;
        let ontology_id = Self::validate_ontology_id(ontology_id)?;
        let publication_id =
            Self::validate_ontology_publication_id(publication_id)?;
        let revision_id = Self::validate_ontology_revision_id(revision_id)?;
        let operation = self
            .audit
            .begin(
                tenant_id,
                user_id,
                AuditTarget::new(
                    AuditAction::RetireOntology,
                    "ontology",
                    ontology_id,
                )
                .with_publication_id(publication_id)
                .with_revision_id(revision_id),
                context,
            )
            .await?;
        match self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                Permission::Delete,
                "ontology",
                ontology_id,
                Some(context),
            )
            .await
        {
            Ok(true) => {},
            Ok(false) => {
                operation.denied("authorization_denied").await?;
                bail!("Authorization denied");
            },
            Err(error) => {
                operation.failed("authorization_dependency_failed").await?;
                return Err(error);
            },
        }
        if let Err(error) = self
            .store
            .retire_ontology(
                tenant_id,
                ontology_id,
                publication_id,
                revision_id,
            )
            .await
        {
            operation.failed("database_mutation_failed").await?;
            return Err(error);
        }
        operation.succeeded().await
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

    /// Lists current tenant users for authorized administrators.
    pub async fn users(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<IamUser>> {
        self.require_tenant_admin(tenant_id, user_id, context)
            .await?;
        self.iam.list_users(tenant_id, bounded(limit)).await
    }

    /// Lists current tenant roles for authorized administrators.
    pub async fn roles(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<IamRole>> {
        self.require_tenant_admin(tenant_id, user_id, context)
            .await?;
        self.iam.list_roles(tenant_id, bounded(limit)).await
    }

    /// Lists current user-to-role assignments for administrators.
    pub async fn role_assignments(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<RoleAssignment>> {
        self.require_tenant_admin(tenant_id, user_id, context)
            .await?;
        self.iam
            .list_role_assignments(tenant_id, bounded(limit))
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

    /// Lists ontology catalog entries visible to the principal.
    pub async fn ontologies(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        limit: usize,
    ) -> Result<Vec<OntologyCatalogEntry>> {
        self.verify_user(tenant_id, user_id).await?;
        let candidates = self
            .store
            .list_ontologies(tenant_id, bounded(limit))
            .await?;
        let mut allowed = Vec::with_capacity(candidates.len());
        for candidate in candidates {
            if self
                .can_view(
                    tenant_id,
                    user_id,
                    context,
                    "ontology",
                    &candidate.ontology_id,
                )
                .await?
            {
                allowed.push(candidate);
            }
        }
        Ok(allowed)
    }

    /// Lists immutable publication history for one visible ontology.
    pub async fn ontology_publication_history(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        ontology_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyPublication>> {
        self.verify_user(tenant_id, user_id).await?;
        if !self
            .can_view(tenant_id, user_id, context, "ontology", ontology_id)
            .await?
        {
            bail!("Authorization denied");
        }
        self.store
            .ontology_publication_history(
                tenant_id,
                ontology_id,
                bounded(limit),
            )
            .await
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
                allowed.push(binding);
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

/// Applies the shared hard bound for control-plane collection reads.
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
mod tests {
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, Ordering};

    use anyhow::{Result, anyhow, ensure};

    use super::*;
    use crate::application::ports::iam_store::{
        IamRole, IamUser, RoleAssignment,
    };
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization, audit, identity,
    };

    struct EmptyIamStore;

    #[async_trait::async_trait]
    impl IamStore for EmptyIamStore {
        async fn create_user(&self, _: &str, _: &str, _: bool) -> Result<()> {
            Ok(())
        }

        async fn update_user(&self, _: &str, _: &str, _: bool) -> Result<()> {
            Ok(())
        }

        async fn delete_user(&self, _: &str, _: &str) -> Result<()> {
            Ok(())
        }

        async fn create_role(&self, _: &str, _: &str) -> Result<()> {
            Ok(())
        }

        async fn delete_role(&self, _: &str, _: &str) -> Result<()> {
            Ok(())
        }

        async fn assign_role_to_user(
            &self,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn unassign_role_from_user(
            &self,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn list_users(&self, _: &str, _: usize) -> Result<Vec<IamUser>> {
            Ok(Vec::new())
        }

        async fn list_roles(&self, _: &str, _: usize) -> Result<Vec<IamRole>> {
            Ok(Vec::new())
        }

        async fn list_role_assignments(
            &self,
            _: &str,
            _: usize,
        ) -> Result<Vec<RoleAssignment>> {
            Ok(Vec::new())
        }

        async fn role_names_for_user(
            &self,
            _: &str,
            _: &str,
        ) -> Result<Vec<String>> {
            Ok(Vec::new())
        }

        async fn user_ids_for_role(
            &self,
            _: &str,
            _: &str,
        ) -> Result<Vec<String>> {
            Ok(Vec::new())
        }

        async fn get_active_cedar_policies(
            &self,
            _: &str,
        ) -> Result<Option<String>> {
            Ok(None)
        }

        async fn upsert_cedar_policy(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: bool,
        ) -> Result<()> {
            Ok(())
        }
    }

    #[derive(Default)]
    struct MemoryControlPlaneStore {
        exists: AtomicBool,
        publication: Mutex<Option<OntologyPublication>>,
        retired: Mutex<Option<(String, String)>>,
    }

    #[async_trait::async_trait]
    impl ControlPlaneStore for MemoryControlPlaneStore {
        async fn publish_ontology(
            &self,
            _: &str,
            _: &str,
            _: &str,
            publication_id: &str,
            revision_id: &str,
            metadata: &serde_json::Value,
            create_only: bool,
        ) -> Result<OntologyPublication> {
            ensure!(
                create_only != self.exists.swap(true, Ordering::AcqRel),
                "unexpected ontology create mode"
            );
            let publication = OntologyPublication {
                publication_id: publication_id.to_owned(),
                revision_id: revision_id.to_owned(),
                lifecycle: "production".to_owned(),
                metadata: metadata.clone(),
                base_version: "base-v1".to_owned(),
                base_hash: "base-hash".to_owned(),
                effective_hash: "effective-hash".to_owned(),
                author: "user_a".to_owned(),
                message: "publish".to_owned(),
                published_at_ms: 1,
                retired_at_ms: None,
            };
            *self.publication.lock().map_err(|error| {
                anyhow!("control-plane test lock poisoned: {error}")
            })? = Some(publication.clone());
            Ok(publication)
        }

        async fn retire_ontology(
            &self,
            _: &str,
            _: &str,
            publication_id: &str,
            revision_id: &str,
        ) -> Result<()> {
            let current = self
                .publication
                .lock()
                .map_err(|error| {
                    anyhow!("control-plane test lock poisoned: {error}")
                })?
                .clone()
                .context("publication missing")?;
            ensure!(current.publication_id == publication_id);
            ensure!(current.revision_id == revision_id);
            *self.retired.lock().map_err(|error| {
                anyhow!("control-plane test lock poisoned: {error}")
            })? = Some((publication_id.to_owned(), revision_id.to_owned()));
            Ok(())
        }

        async fn ontology_exists(&self, _: &str, _: &str) -> Result<bool> {
            Ok(self.exists.load(Ordering::Acquire))
        }

        async fn list_ontologies(
            &self,
            _: &str,
            _: usize,
        ) -> Result<Vec<OntologyCatalogEntry>> {
            Ok(Vec::new())
        }

        async fn ontology_publication_history(
            &self,
            _: &str,
            _: &str,
            _: usize,
        ) -> Result<Vec<OntologyPublication>> {
            Ok(Vec::new())
        }

        async fn list_ontology_bindings(
            &self,
            _: &str,
            _: Option<&str>,
            _: usize,
        ) -> Result<Vec<PipelineOntologyBinding>> {
            Ok(Vec::new())
        }

        async fn list_pipeline_executions(
            &self,
            _: &str,
            _: Option<&str>,
            _: usize,
        ) -> Result<Vec<PipelineExecution>> {
            Ok(Vec::new())
        }
    }

    #[test]
    fn control_plane_reads_have_a_hard_limit() {
        assert_eq!(bounded(0), 1);
        assert_eq!(bounded(50), 50);
        assert_eq!(bounded(usize::MAX), HARD_LIMIT);
    }

    #[test]
    fn ontology_identifiers_match_authoritative_runtime_grammar() {
        assert!(
            ControlPlaneService::validate_ontology_id("risk:case-v1").is_ok()
        );
        assert!(
            ControlPlaneService::validate_ontology_id("risk/case").is_err()
        );
        assert!(
            ControlPlaneService::validate_ontology_revision_id(
                "0123456789abcdef0123456789abcdef"
            )
            .is_ok()
        );
        assert!(
            ControlPlaneService::validate_ontology_revision_id(
                "0123456789ABCDEF0123456789ABCDEF"
            )
            .is_err()
        );
        assert!(
            ControlPlaneService::validate_ontology_publication_id(
                "fedcba9876543210fedcba9876543210"
            )
            .is_ok()
        );
    }

    #[tokio::test]
    async fn ontology_publication_and_retirement_preserve_provenance()
    -> Result<()> {
        let store = Arc::new(MemoryControlPlaneStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, audit_store) = audit();
        let service = ControlPlaneService::new(
            store.clone(),
            Arc::new(EmptyIamStore),
            identity(true),
            authorization.clone(),
            audit,
        );
        let context = QueryContext {
            request_id: "request-ontology".to_owned(),
            ..QueryContext::default()
        };
        let revision_id = "0123456789abcdef0123456789abcdef";
        let first = service
            .publish_ontology(
                "tenant_a",
                "user_a",
                &context,
                "risk:case-v1",
                "Risk case",
                revision_id,
                &serde_json::json!({"source": "ontology"}),
            )
            .await?;
        service
            .retire_ontology(
                "tenant_a",
                "user_a",
                &context,
                "risk:case-v1",
                &first.publication_id,
                revision_id,
            )
            .await?;

        ensure!(
            store
                .retired
                .lock()
                .map_err(|error| anyhow!(
                    "control-plane test lock poisoned: {error}"
                ))?
                .as_ref() ==
                Some(&(
                    first.publication_id.clone(),
                    revision_id.to_owned()
                ))
        );
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
        ensure!(events.len() == 4);
        ensure!(events.iter().all(|event| {
            event.revision_id.as_deref() == Some(revision_id) &&
                event.publication_id.as_deref() ==
                    Some(first.publication_id.as_str())
        }));
        Ok(())
    }
}
