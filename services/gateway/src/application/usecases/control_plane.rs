//! Authorized control-plane reads over canonical platform records.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result, bail};

use crate::application::ports::audit_store::{AuditEvent, AuditFilter};
use crate::application::ports::control_plane_store::{
    ControlPlaneStore, OntologyCatalogEntry, OntologyPublication,
    PipelineExecution, PipelineOntologyBinding,
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
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
}

impl ControlPlaneService {
    /// Uses the same granular SpiceDB/Cedar decision for UI hints and writes.
    pub async fn ontology_permission(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: Option<&str>,
        permission: Permission,
    ) -> Result<bool> {
        self.verify_user(tenant, user).await?;
        let (kind, resource) = match (permission, id) {
            (Permission::CreateOntology, None) => ("tenant", tenant),
            (Permission::Edit | Permission::Publish, Some(id)) => {
                Self::validate_ontology_id(id)?;
                if !self
                    .can_view(tenant, user, context, "ontology", id)
                    .await?
                {
                    return Ok(false);
                }
                ("ontology", id)
            },
            _ => anyhow::bail!("Invalid ontology permission request"),
        };
        self.auth
            .is_authorized(
                user,
                tenant,
                permission,
                kind,
                resource,
                Some(context),
            )
            .await
    }

    /// Checks binding authority before any related pipeline draft is modified.
    pub async fn authorize_ontology_binding(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        pipeline: &str,
    ) -> Result<()> {
        self.verify_user(tenant, user).await?;
        anyhow::ensure!(
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
            "Pipeline edit access denied"
        );
        anyhow::ensure!(
            self.can_view(tenant, user, context, "ontology", id).await?,
            "Ontology access denied"
        );
        anyhow::ensure!(
            self.store.list_ontologies(tenant, 100).await?.iter().any(
                |item| item.ontology_id == id &&
                    item.production_publication.as_ref().is_some_and(
                        |publication| publication.lifecycle == "production"
                    )
            ),
            "Ontology must be published before binding"
        );
        Ok(())
    }

    /// Binding changes are tenant administration and never activate a
    /// pipeline.
    #[expect(
        clippy::too_many_arguments,
        reason = "Keeps the authenticated actor and pinned binding identity explicit, matching publication operations"
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
        Self::validate_ontology_id(ontology)?;
        anyhow::ensure!(
            self.can_view(tenant, user, context, "ontology", ontology)
                .await?,
            "Ontology access denied"
        );
        let operation = self
            .audit
            .begin(
                tenant,
                user,
                AuditTarget::new(
                    AuditAction::BindOntology,
                    "ontology",
                    ontology,
                ),
                context,
            )
            .await?;
        match self
            .store
            .bind_ontology(tenant, user, pipeline, block, ontology, expected)
            .await
        {
            Ok(head) => {
                operation.succeeded().await?;
                Ok(head)
            },
            Err(error) => {
                operation.failed("binding_write_failed").await?;
                Err(error)
            },
        }
    }

    /// Captures the editable tenant snapshot without selecting an unpinned
    /// target.
    pub async fn ontology_workspace(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
    ) -> Result<serde_json::Value> {
        self.verify_user(tenant, user).await?;
        Self::validate_ontology_id(id)?;
        if !self.can_view(tenant, user, context, "ontology", id).await? {
            bail!("Authorization denied");
        }
        let head = self.store.ontology_head(tenant).await?;
        let document = self.store.ontology_document(tenant, id, &head).await?;
        Ok(serde_json::json!({"revision_id":head,"ontology":document}))
    }

    /// Reads a pinned document without widening catalog visibility.
    pub async fn ontology_document(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        revision: &str,
    ) -> Result<serde_json::Value> {
        self.verify_user(tenant, user).await?;
        Self::validate_ontology_id(id)?;
        Self::validate_ontology_revision_id(revision)?;
        if !self.can_view(tenant, user, context, "ontology", id).await? {
            bail!("Authorization denied");
        }
        self.store.ontology_document(tenant, id, revision).await
    }

    /// Stores an editable tenant copy; publishing remains a separate
    /// operation.
    #[expect(
        clippy::too_many_arguments,
        reason = "Keeps tenant, actor, content and compare-and-swap revision explicit at the authorization boundary"
    )]
    pub async fn save_plugin_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        name: &str,
        ontology: &serde_json::Value,
        expected: Option<&str>,
    ) -> Result<String> {
        let (id_for_permission, permission) = if expected.is_some() {
            (Some(id), Permission::Edit)
        } else {
            (None, Permission::CreateOntology)
        };
        anyhow::ensure!(
            self.ontology_permission(
                tenant,
                user,
                context,
                id_for_permission,
                permission
            )
            .await?,
            "Ontology write access denied"
        );
        Self::validate_ontology_id(id)?;
        anyhow::ensure!(
            !name.trim().is_empty() && name.len() <= 256,
            "Invalid ontology name"
        );
        galadril_registry::semantics::validate_ontology(ontology)?;
        let operation = self
            .audit
            .begin(
                tenant,
                user,
                AuditTarget::new(
                    AuditAction::SaveOntologyDraft,
                    "ontology",
                    id,
                ),
                context,
            )
            .await?;
        let revision = match self
            .store
            .save_ontology(tenant, id, name, user, ontology, expected)
            .await
        {
            Ok(revision) => revision,
            Err(error) => {
                operation.failed("ontology_write_failed").await?;
                return Err(error);
            },
        };
        if expected.is_none() {
            for (relation, kind, subject) in
                [("parent", "tenant", tenant), ("owner", "user", user)]
            {
                if let Err(error) = self
                    .auth
                    .upsert_relationship(
                        "ontology",
                        &format!("{tenant}/{id}"),
                        relation,
                        kind,
                        subject,
                    )
                    .await
                {
                    operation.failed("ontology_acl_write_failed").await?;
                    return Err(error);
                }
            }
        }
        operation.succeeded().await?;
        Ok(revision)
    }

    /// Creates the control-plane service from authoritative domain ports.
    pub fn new(
        store: Arc<dyn ControlPlaneStore>,
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

    /// Accepts opaque immutable Registry revision identifiers.
    fn validate_ontology_revision_id(revision_id: &str) -> Result<&str> {
        if !(20..=128).contains(&revision_id.len()) ||
            !galadril_registry::storage::valid_revision(revision_id)
        {
            bail!(
                "Ontology revision identifier must be an opaque Registry revision"
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
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization, audit, identity,
    };

    #[derive(Default)]
    struct MemoryControlPlaneStore {
        exists: AtomicBool,
        publication: Mutex<Option<OntologyPublication>>,
        retired: Mutex<Option<(String, String)>>,
        drafts: Mutex<HashMap<(String, String), serde_json::Value>>,
    }

    #[async_trait::async_trait]
    impl ControlPlaneStore for MemoryControlPlaneStore {
        async fn save_ontology(
            &self,
            tenant: &str,
            id: &str,
            _: &str,
            _: &str,
            document: &serde_json::Value,
            _: Option<&str>,
        ) -> Result<String> {
            self.drafts
                .lock()
                .map_err(|_| anyhow!("poisoned test store"))?
                .insert((tenant.into(), id.into()), document.clone());
            Ok("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into())
        }

        async fn ontology_document(
            &self,
            tenant: &str,
            id: &str,
            _: &str,
        ) -> Result<serde_json::Value> {
            self.drafts
                .lock()
                .map_err(|_| anyhow!("poisoned test store"))?
                .get(&(tenant.into(), id.into()))
                .cloned()
                .context("ontology unavailable")
        }

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
            .is_ok()
        );
        assert!(
            ControlPlaneService::validate_ontology_publication_id(
                "fedcba9876543210fedcba9876543210"
            )
            .is_ok()
        );
    }

    #[tokio::test]
    async fn ontology_writes_do_not_inherit_pipeline_or_tenant_management_grants()
    -> Result<()> {
        for (permission, expected) in [
            (Permission::CreateOntology, None),
            (Permission::Edit, Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")),
        ] {
            let store = Arc::new(MemoryControlPlaneStore::default());
            let auth = TestAuthorization::new(AuthorizationDecision::Allow);
            auth.denied_permissions
                .lock()
                .map_err(|_| anyhow!("test lock poisoned"))?
                .push(permission);
            assert!(
                auth.is_authorized(
                    "user_a",
                    "tenant_a",
                    Permission::CreatePipeline,
                    "tenant",
                    "tenant_a",
                    None
                )
                .await?
            );
            let (auditor, _) = audit();
            let service = ControlPlaneService::new(
                store.clone(),
                identity(true),
                auth,
                auditor,
            );
            let document = serde_json::json!({"version":"1","resources":[]});
            assert!(
                service
                    .save_plugin_ontology(
                        "tenant_a",
                        "user_a",
                        &QueryContext::default(),
                        "mail",
                        "Mail",
                        &document,
                        expected
                    )
                    .await
                    .is_err()
            );
            assert!(
                store
                    .drafts
                    .lock()
                    .map_err(|_| anyhow!("test lock poisoned"))?
                    .is_empty()
            );
        }
        Ok(())
    }

    #[tokio::test]
    async fn ontology_overlay_writes_require_tenant_authority_and_remain_scoped()
    -> Result<()> {
        let store = Arc::new(MemoryControlPlaneStore::default());
        let (auditor, _) = audit();
        let denied = ControlPlaneService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            auditor,
        );
        let document = serde_json::json!({"version":"1","resources":[{"resource_id":"mail.person","kind":"object_type","display_name":"Plugin Person","references":[],"attributes":{}}]});
        assert!(
            denied
                .save_plugin_ontology(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    "mail",
                    "Mail",
                    &document,
                    None
                )
                .await
                .is_err()
        );
        assert!(
            store
                .drafts
                .lock()
                .map_err(|_| anyhow!("poisoned test store"))?
                .is_empty()
        );
        let (auditor, _) = audit();
        let allowed = ControlPlaneService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Allow),
            auditor,
        );
        allowed
            .save_plugin_ontology(
                "tenant_a",
                "user_a",
                &QueryContext::default(),
                "mail",
                "Mail",
                &document,
                None,
            )
            .await?;
        let mut other = document.clone();
        other
            .get_mut("resources")
            .and_then(serde_json::Value::as_array_mut)
            .and_then(|items| items.first_mut())
            .and_then(serde_json::Value::as_object_mut)
            .context("test participant missing")?
            .insert(
                "display_name".into(),
                serde_json::json!("Tenant B participant"),
            );
        allowed
            .save_plugin_ontology(
                "tenant_b",
                "user_a",
                &QueryContext::default(),
                "mail",
                "Mail",
                &other,
                None,
            )
            .await?;
        assert_eq!(
            store.ontology_document("tenant_a", "mail", "").await?,
            document
        );
        assert_eq!(
            store.ontology_document("tenant_b", "mail", "").await?,
            other
        );
        Ok(())
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
