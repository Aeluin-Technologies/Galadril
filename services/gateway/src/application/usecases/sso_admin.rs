//! Tenant-owned federation management and explicit permission imports.

use std::sync::Arc;

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::application::ports::federation::{
    FederationConfig, FederationStore,
};
use crate::application::ports::sso::{Organization, SsoBroker, SsoStore};
use crate::application::usecases::audit::{
    AuditAction, AuditOperation, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, QueryContext, validate_cedar_policy,
};
use crate::application::usecases::iam_admin::{
    DataDomainGrant, DataDomainGrantRequest, IamAdminService,
};
use crate::domain::sso::{
    ConnectionInput, PermissionManifest, PermissionOperation, ProviderPreview,
    SsoSettings, validate_domain,
};

pub struct SsoAdminService {
    store: Arc<dyn SsoStore>,
    broker: Arc<dyn SsoBroker>,
    iam: Arc<IamAdminService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
    federation: Option<Arc<dyn FederationStore>>,
}

#[derive(Serialize)]
pub struct DomainChallenge {
    pub record: String,
    pub value: String,
}

#[derive(Serialize)]
pub struct ImportResult {
    pub applied: usize,
    pub failed_at: Option<usize>,
    pub complete: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProvisionRequest {
    pub request_id: String,
    pub owner_external_subject: String,
    pub connection: ConnectionInput,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalProvisionRequest {
    pub request_id: String,
    pub owner_external_subject: String,
}

impl SsoAdminService {
    /// Keeps metadata inspection inside the tenant administrator audit
    /// boundary.
    pub async fn detect_provider(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        domain: &str,
        address: &str,
    ) -> Result<ProviderPreview> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureSso)
            .await?;
        let result = async {
            validate_domain(domain)?;
            ensure!(
                !address.is_empty() && address.len() <= 2048,
                "Invalid discovery address"
            );
            let proof = self.store.proof(tenant, domain).await?;
            let domain_verified =
                match self.broker.verify_domain(domain, &proof).await {
                    Ok(()) => true,
                    Err(_) => {
                        tracing::info!(
                            event.name = "sso.domain.verification_pending",
                            "domain verification remains pending"
                        );
                        false
                    },
                };
            let configuration =
                self.broker.detect(address).await.map_err(|_| {
                    anyhow::anyhow!("Provider metadata could not be verified")
                })?;
            tracing::info!(
                event.name = "sso.provider.detected",
                domain_verified,
                "provider metadata inspected"
            );
            Ok(ProviderPreview {
                domain: domain.to_owned(),
                domain_verified,
                configuration,
            })
        }
        .await;
        Self::finish(operation, result).await
    }

    /// Checks current authentication state before executing tenant operations.
    pub async fn validate_session(
        &self,
        tenant: &str,
        user: &str,
        assurance: Option<&str>,
        generation: Option<i64>,
    ) -> Result<bool> {
        self.store
            .session_allowed(tenant, user, assurance, generation)
            .await
    }

    /// Updates a bounded display name through the same tenant authorization
    /// boundary.
    pub async fn update_organization(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        name: &str,
    ) -> Result<()> {
        let name = name.trim();
        ensure!(
            !name.is_empty() &&
                name.chars().count() <= 128 &&
                !name.chars().any(char::is_control),
            "Invalid organization name"
        );
        let operation = self
            .authorize(
                tenant,
                user,
                context,
                AuditAction::ConfigureOrganization,
            )
            .await?;
        Self::finish(
            operation,
            self.store.organization_name(tenant, name).await,
        )
        .await
    }

    /// Shares the existing IAM authorization and audit services.
    pub fn new(
        store: Arc<dyn SsoStore>,
        broker: Arc<dyn SsoBroker>,
        iam: Arc<IamAdminService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
    ) -> Self {
        Self {
            store,
            broker,
            iam,
            auth,
            audit,
            federation: None,
        }
    }

    /// Shares the tenant-bound configuration store with the identity broker.
    pub fn with_federation_store(
        mut self,
        federation: Arc<dyn FederationStore>,
    ) -> Self {
        self.federation = Some(federation);
        self
    }

    /// Returns only administrator-approved mappings belonging to this tenant.
    pub async fn role_sync_configurations(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
    ) -> Result<Vec<FederationConfig>> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureRoleSync)
            .await?;
        let result = async {
            self.federation
                .as_ref()
                .ok_or_else(|| {
                    anyhow::anyhow!("Role synchronization is unavailable")
                })?
                .configurations(tenant)
                .await
        }
        .await;
        Self::finish(operation, result).await
    }

    /// Creates only explicitly approved role names before enabling claim
    /// import.
    pub async fn configure_role_sync(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        configuration: &FederationConfig,
    ) -> Result<()> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureRoleSync)
            .await?;
        let result=async {
            configuration.validate()?;
            let settings=self.store.settings(tenant).await?;
            ensure!(!configuration.enabled || settings.permission_import,"Permission import disabled");
            ensure!(settings.connections.iter().any(|connection|connection.enabled && connection.connection_id==configuration.connection_id),"Choose an active sign-in connection belonging to this organization");
            let federation=self.federation.as_ref().ok_or_else(||anyhow::anyhow!("Role synchronization is unavailable"))?;
            let mut names=std::collections::HashSet::with_capacity(configuration.mappings.len());
            // Only explicit administrator-approved mapping names reach the same
            // audited, quota-enforced role creator as manual administration.
            for mapping in &configuration.mappings {names.insert(mapping.role_name.as_str());}
            let names=names.into_iter().collect::<Vec<_>>();
            self.iam.create_roles(tenant,user,context,&names).await?;
            federation.configure(tenant,configuration).await
        }.await;
        Self::finish(operation, result).await
    }

    async fn authorize(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        action: AuditAction,
    ) -> Result<AuditOperation> {
        self.iam
            .begin_authorized_operation(
                tenant,
                user,
                context,
                AuditTarget::new(action, "tenant", tenant),
            )
            .await
    }

    async fn finish<T>(
        operation: AuditOperation,
        result: Result<T>,
    ) -> Result<T> {
        match result {
            Ok(value) => {
                operation.succeeded().await?;
                Ok(value)
            },
            Err(error) => {
                operation.failed("sso_operation_failed").await?;
                Err(error)
            },
        }
    }

    /// Returns write-only credential metadata only to tenant administrators.
    pub async fn settings(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
    ) -> Result<SsoSettings> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureSso)
            .await?;
        Self::finish(operation, self.store.settings(tenant).await).await
    }

    /// Requires DNS ownership before an email domain can select an IdP.
    pub async fn request_domain(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        domain: &str,
    ) -> Result<DomainChallenge> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureSso)
            .await?;
        let result = async {
            validate_domain(domain)?;
            let proof = Uuid::new_v4().simple().to_string() +
                &Uuid::new_v4().simple().to_string();
            self.store.challenge(tenant, domain, &proof).await?;
            Ok(DomainChallenge {
                record: format!("_galadril.{domain}"),
                value: format!("galadril-verification={proof}"),
            })
        }
        .await;
        Self::finish(operation, result).await
    }

    /// Rotates provider credentials through encrypted Polis, preserving
    /// identity IDs.
    pub async fn configure(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        input: &ConnectionInput,
    ) -> Result<()> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureSso)
            .await?;
        let result = async {
            input.validate()?;
            if !self
                .store
                .settings(tenant)
                .await?
                .connections
                .iter()
                .any(|connection| connection.domain == input.domain)
            {
                let proof = self.store.proof(tenant, &input.domain).await?;
                self.broker.verify_domain(&input.domain, &proof).await?;
            }
            let existing =
                self.store.claim_domain(tenant, &input.domain).await?;
            let connection = self
                .broker
                .configure(tenant, input, existing.as_deref())
                .await?;
            self.store.connection(tenant, input, &connection).await
        }
        .await;
        Self::finish(operation, result).await
    }

    /// Disables new logins without removing durable external identity
    /// mappings.
    pub async fn disable(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        domain: &str,
    ) -> Result<()> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureSso)
            .await?;
        Self::finish(operation, self.store.disable(tenant, domain).await).await
    }

    /// Preserves canonical IAM accounts instead of trusting mutable email
    /// addresses.
    pub async fn link_identity(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        domain: &str,
        subject: &str,
        target_user: &str,
    ) -> Result<()> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ConfigureSso)
            .await?;
        let result = async {
            validate_domain(domain)?;
            crate::domain::sso::validate_identifier(target_user)?;
            ensure!(
                !subject.is_empty() && subject.len() <= 1024,
                "Invalid external subject"
            );
            self.store
                .link_identity(tenant, domain, subject, target_user)
                .await
        }
        .await;
        Self::finish(operation, result).await
    }

    /// Lets tenant administrators opt in through the same API as automation.
    pub async fn set_import(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        enabled: bool,
    ) -> Result<()> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::SetPermissionImport)
            .await?;
        Self::finish(
            operation,
            self.store.import_setting(tenant, enabled).await,
        )
        .await
    }

    /// Lets tenant administrators require a verified second factor.
    pub async fn set_passkey_mfa(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        enabled: bool,
    ) -> Result<()> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::SetPasskeyMfa)
            .await?;
        Self::finish(
            operation,
            self.store.passkey_setting(tenant, enabled).await,
        )
        .await
    }

    /// Recovery requires tenant administration rather than possession of an
    /// email address.
    pub async fn reset_passkeys(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        target: &str,
    ) -> Result<()> {
        crate::domain::sso::validate_identifier(target)?;
        let operation = self
            .iam
            .begin_authorized_operation(
                tenant,
                user,
                context,
                AuditTarget::new(AuditAction::ResetPasskeys, "user", target),
            )
            .await?;
        Self::finish(
            operation,
            self.store.reset_passkeys(tenant, target).await,
        )
        .await
    }

    /// Validates the entire batch, then applies native audited IAM operations.
    pub async fn import(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        manifest: &PermissionManifest,
    ) -> Result<ImportResult> {
        let operation = self
            .authorize(tenant, user, context, AuditAction::ImportPermissions)
            .await?;
        let result = async {
            ensure!(
                self.store.settings(tenant).await?.permission_import,
                "Permission import disabled"
            );
            manifest.validate()?;
            for change in &manifest.operations {
                if let PermissionOperation::CedarPolicy { content, .. } =
                    change
                {
                    validate_cedar_policy(content)?;
                }
            }
            for (index, change) in manifest.operations.iter().enumerate() {
                let applied = match change {
                    PermissionOperation::RoleAssignment {
                        user: subject,
                        role,
                        enabled,
                    } => {
                        if *enabled {
                            self.iam
                                .assign_role_to_user(
                                    tenant, user, context, subject, role,
                                )
                                .await
                        } else {
                            self.iam
                                .unassign_role_from_user(
                                    tenant, user, context, subject, role,
                                )
                                .await
                        }
                    },
                    PermissionOperation::Relationship {
                        resource,
                        relation,
                        subject,
                        enabled,
                    } => {
                        self.iam
                            .set_data_domain_grant(
                                tenant,
                                user,
                                context,
                                DataDomainGrantRequest {
                                    group_id: &resource.id,
                                    subject_user_id: &subject.id,
                                    grant: if relation == "reader" {
                                        DataDomainGrant::Read
                                    } else {
                                        DataDomainGrant::Ingest
                                    },
                                    enabled: *enabled,
                                },
                            )
                            .await
                    },
                    PermissionOperation::CedarPolicy {
                        id,
                        content,
                        active,
                    } => {
                        self.iam
                            .set_cedar_policy(
                                tenant, user, context, id, content, *active,
                            )
                            .await
                    },
                };
                if applied.is_err() {
                    tracing::warn!(
                        event.name = "sso.permission_import.stopped",
                        applied = index,
                        "permission import stopped"
                    );
                    return Ok(ImportResult {
                        applied: index,
                        failed_at: Some(index),
                        complete: false,
                    });
                }
            }
            Ok(ImportResult {
                applied: manifest.operations.len(),
                failed_at: None,
                complete: true,
            })
        }
        .await;
        match result {
            Ok(value) if !value.complete => {
                operation.failed("permission_import_partial").await?;
                Ok(value)
            },
            result => Self::finish(operation, result).await,
        }
    }

    /// Initializes only a new organization for an authenticated provisioning
    /// process.
    pub async fn provision(
        &self,
        request: &ProvisionRequest,
        context: &QueryContext,
    ) -> Result<Organization> {
        request.connection.validate()?;
        self.enroll(
            &request.request_id,
            &request.owner_external_subject,
            Some(&request.connection),
            context,
        )
        .await
    }

    /// Enrolls a local test owner without claiming an enterprise email domain.
    pub async fn provision_local(
        &self,
        request: &LocalProvisionRequest,
        context: &QueryContext,
    ) -> Result<Organization> {
        Uuid::parse_str(&request.owner_external_subject)?;
        self.enroll(
            &request.request_id,
            &request.owner_external_subject,
            None,
            context,
        )
        .await
    }

    async fn enroll(
        &self,
        request_id: &str,
        external_subject: &str,
        input: Option<&ConnectionInput>,
        context: &QueryContext,
    ) -> Result<Organization> {
        let request_uuid = Uuid::parse_str(request_id)?;
        let local_domain = format!("{request_uuid}.demo.invalid");
        let domain = input.map_or(local_domain.as_str(), |connection| {
            connection.domain.as_str()
        });
        ensure!(
            !external_subject.is_empty() && external_subject.len() <= 1024,
            "Invalid external owner subject"
        );
        let mut organization = self
            .store
            .reserve(request_id, domain, external_subject)
            .await?;
        if organization.ready {
            return Ok(organization);
        }
        // Provisioning retries need correlation even without HTTP request
        // metadata.
        let fallback_context =
            context.request_id.is_empty().then(|| QueryContext {
                request_id: request_id.to_owned(),
                ..context.clone()
            });
        let context = fallback_context.as_ref().unwrap_or(context);
        let operation = self
            .audit
            .begin(
                &organization.tenant_id,
                "organization_provisioner",
                AuditTarget::new(
                    AuditAction::ProvisionOrganization,
                    "tenant",
                    &organization.tenant_id,
                ),
                context,
            )
            .await?;
        let result = async {
            self.broker
                .initialize_organization(&organization.tenant_id)
                .await?;
            let connection = if let Some(input) = input {
                let existing = self
                    .store
                    .claim_domain(&organization.tenant_id, &input.domain)
                    .await?;
                self.broker
                    .configure(
                        &organization.tenant_id,
                        input,
                        existing.as_deref(),
                    )
                    .await?
            } else {
                "kratos".to_owned()
            };
            self.store
                .owner(&organization, &connection, external_subject)
                .await?;
            self.auth
                .upsert_relationship(
                    "tenant",
                    &organization.tenant_id,
                    "administrator",
                    "user",
                    &organization.owner_id,
                )
                .await?;
            self.auth
                .invalidate_tenant_cache(&organization.tenant_id)
                .await;
            if let Some(input) = input {
                self.store
                    .connection(&organization.tenant_id, input, &connection)
                    .await?;
            }
            self.store.complete(&organization).await?;
            Ok(())
        };
        let result = match tokio::time::timeout(
            std::time::Duration::from_secs(45),
            result,
        )
        .await
        {
            Ok(result) => result,
            Err(_) => {
                Err(anyhow::anyhow!("Organization enrollment timed out"))
            },
        };
        if result.is_err() &&
            let Err(error) = self.store.release(&organization).await
        {
            tracing::error!(
                event.name = "sso.enrollment.release_failed",
                "Enrollment lease will expire automatically"
            );
            return Self::finish(operation, Err(error)).await;
        }
        organization.ready = result.is_ok();
        Self::finish(operation, result.map(|()| organization)).await
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    use super::*;
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization, audit, identity,
    };
    use crate::application::usecases::iam_admin::tests::MemoryIamStore;

    #[derive(Default)]
    struct Store {
        import: AtomicBool,
        writes: AtomicUsize,
        role_sync_connection: AtomicBool,
    }

    #[async_trait::async_trait]
    impl SsoStore for Store {
        async fn session_allowed(
            &self,
            _tenant: &str,
            _user: &str,
            _assurance: Option<&str>,
            _generation: Option<i64>,
        ) -> Result<bool> {
            Ok(true)
        }

        async fn organization_name(
            &self,
            tenant: &str,
            name: &str,
        ) -> Result<()> {
            ensure!(
                tenant == "acme" && name == "Acme",
                "Wrong tenant or name"
            );
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn reserve(
            &self,
            _request: &str,
            _domain: &str,
            _subject: &str,
        ) -> Result<Organization> {
            Ok(Organization {
                tenant_id: "acme".into(),
                owner_id: "owner".into(),
                ready: false,
                enrollment_token: "lease".into(),
            })
        }

        async fn settings(&self, tenant: &str) -> Result<SsoSettings> {
            ensure!(tenant == "acme", "Wrong tenant");
            Ok(SsoSettings {
                organization_name: String::new(),
                permission_import: self.import.load(Ordering::SeqCst),
                passkey_mfa: false,
                connections: if self
                    .role_sync_connection
                    .load(Ordering::SeqCst)
                {
                    vec![crate::domain::sso::ConnectionView {
                        connection_id: "connection".into(),
                        domain: "acme.example".into(),
                        protocol: "oidc".into(),
                        enabled: true,
                        jit_provision: false,
                    }]
                } else {
                    Vec::new()
                },
            })
        }

        async fn challenge(
            &self,
            tenant: &str,
            _domain: &str,
            _proof: &str,
        ) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            Ok(())
        }

        async fn proof(&self, tenant: &str, _domain: &str) -> Result<String> {
            ensure!(tenant == "acme", "Wrong tenant");
            Ok("proof".into())
        }

        async fn claim_domain(
            &self,
            tenant: &str,
            _domain: &str,
        ) -> Result<Option<String>> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(None)
        }

        async fn connection(
            &self,
            tenant: &str,
            _input: &ConnectionInput,
            _connection: &str,
        ) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn import_setting(
            &self,
            tenant: &str,
            enabled: bool,
        ) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.import.store(enabled, Ordering::SeqCst);
            Ok(())
        }

        async fn passkey_setting(
            &self,
            tenant: &str,
            _enabled: bool,
        ) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn reset_passkeys(
            &self,
            tenant: &str,
            target: &str,
        ) -> Result<()> {
            ensure!(
                tenant == "acme" && target == "bob",
                "Wrong tenant or target"
            );
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn disable(&self, tenant: &str, _domain: &str) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn owner(
            &self,
            organization: &Organization,
            _connection: &str,
            _subject: &str,
        ) -> Result<()> {
            ensure!(organization.tenant_id == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn complete(&self, organization: &Organization) -> Result<()> {
            ensure!(organization.tenant_id == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        async fn release(&self, organization: &Organization) -> Result<()> {
            ensure!(organization.tenant_id == "acme", "Wrong tenant");
            Ok(())
        }

        async fn link_identity(
            &self,
            tenant: &str,
            _domain: &str,
            _subject: &str,
            _user: &str,
        ) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }

    struct Broker {
        domain_verified: bool,
        configured: AtomicUsize,
    }
    #[async_trait::async_trait]
    impl SsoBroker for Broker {
        async fn detect(
            &self,
            _address: &str,
        ) -> Result<crate::domain::sso::ProviderDiscovery> {
            Ok(crate::domain::sso::ProviderDiscovery::Oidc {
                issuer: "https://idp.example".to_owned(),
                sign_in_url: "https://idp.example/authorize".to_owned(),
                jwks_uri: "https://idp.example/keys".to_owned(),
                automatic_registration: false,
            })
        }

        async fn configure(
            &self,
            tenant: &str,
            _input: &ConnectionInput,
            _existing: Option<&str>,
        ) -> Result<String> {
            ensure!(tenant == "acme", "Wrong tenant");
            self.configured.fetch_add(1, Ordering::SeqCst);
            Ok("connection".into())
        }

        async fn verify_domain(
            &self,
            _domain: &str,
            _proof: &str,
        ) -> Result<()> {
            ensure!(self.domain_verified, "DNS proof missing");
            Ok(())
        }

        async fn initialize_organization(&self, tenant: &str) -> Result<()> {
            ensure!(tenant == "acme", "Wrong tenant");
            Ok(())
        }
    }

    fn service(
        decision: AuthorizationDecision,
        verified: bool,
    ) -> (
        SsoAdminService,
        Arc<Store>,
        Arc<Broker>,
        Arc<TestAuthorization>,
    ) {
        let store = Arc::new(Store::default());
        let broker = Arc::new(Broker {
            domain_verified: verified,
            configured: AtomicUsize::new(0),
        });
        let authorization = TestAuthorization::new(decision);
        let (audit, _) = audit();
        let iam = Arc::new(IamAdminService::new(
            Arc::new(MemoryIamStore::default()),
            identity(true),
            authorization.clone(),
            audit.clone(),
        ));
        (
            SsoAdminService::new(
                store.clone(),
                broker.clone(),
                iam,
                authorization.clone(),
                audit,
            ),
            store,
            broker,
            authorization,
        )
    }

    fn connection() -> Result<ConnectionInput> {
        Ok(serde_json::from_str(
            r#"{"domain":"acme.example","oidc_issuer":"https://login.acme.example"}"#,
        )?)
    }

    struct FederationFixture {
        writes: AtomicUsize,
    }

    #[async_trait::async_trait]
    impl crate::application::ports::federation::FederationStore
        for FederationFixture
    {
        async fn configurations(
            &self,
            _: &str,
        ) -> Result<Vec<crate::authentication::FederationConfig>> {
            Ok(Vec::new())
        }

        async fn configure(
            &self,
            _: &str,
            _: &crate::authentication::FederationConfig,
        ) -> Result<()> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }

    #[tokio::test]
    async fn provider_role_sync_configuration_requires_admin_and_explicit_import_opt_in()
    -> Result<()> {
        let fixture = Arc::new(FederationFixture {
            writes: AtomicUsize::new(0),
        });
        let configuration = serde_json::from_str(
            r#"{"connectionId":"connection","claim":"groups","enabled":true,"mappings":[{"externalGroup":"Research","roleName":"analyst"}]}"#,
        )?;
        let (denied, _, _, _) = service(AuthorizationDecision::Deny, true);
        let denied = denied.with_federation_store(fixture.clone());
        assert!(
            denied
                .configure_role_sync(
                    "acme",
                    "alice",
                    &QueryContext::default(),
                    &configuration
                )
                .await
                .is_err()
        );
        let (allowed, store, _, _) =
            service(AuthorizationDecision::Allow, true);
        let allowed = allowed.with_federation_store(fixture.clone());
        assert!(
            allowed
                .configure_role_sync(
                    "acme",
                    "alice",
                    &QueryContext::default(),
                    &configuration
                )
                .await
                .is_err()
        );
        assert_eq!(fixture.writes.load(Ordering::SeqCst), 0);
        store.import.store(true, Ordering::SeqCst);
        store.role_sync_connection.store(true, Ordering::SeqCst);
        allowed
            .configure_role_sync(
                "acme",
                "alice",
                &QueryContext::default(),
                &configuration,
            )
            .await?;
        assert_eq!(fixture.writes.load(Ordering::SeqCst), 1);
        Ok(())
    }

    #[tokio::test]
    async fn provider_detection_requires_admin_and_never_persists_configuration()
    -> Result<()> {
        let (denied, store, broker, _) =
            service(AuthorizationDecision::Deny, true);
        assert!(
            denied
                .detect_provider(
                    "acme",
                    "alice",
                    &QueryContext::default(),
                    "acme.example",
                    "https://idp.example"
                )
                .await
                .is_err()
        );
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        assert_eq!(broker.configured.load(Ordering::SeqCst), 0);
        let (allowed, store, broker, _) =
            service(AuthorizationDecision::Allow, false);
        let preview = allowed
            .detect_provider(
                "acme",
                "admin",
                &QueryContext::default(),
                "acme.example",
                "https://idp.example",
            )
            .await?;
        assert!(!preview.domain_verified);
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        assert_eq!(broker.configured.load(Ordering::SeqCst), 0);
        Ok(())
    }

    #[tokio::test]
    async fn organization_settings_require_admin_and_a_bounded_display_name()
    -> Result<()> {
        let (denied, store, _, _) = service(AuthorizationDecision::Deny, true);
        assert!(
            denied
                .update_organization(
                    "acme",
                    "alice",
                    &QueryContext::default(),
                    "Acme"
                )
                .await
                .is_err()
        );
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        let (allowed, store, _, _) =
            service(AuthorizationDecision::Allow, true);
        assert!(
            allowed
                .update_organization(
                    "acme",
                    "admin",
                    &QueryContext::default(),
                    "  "
                )
                .await
                .is_err()
        );
        assert!(
            allowed
                .update_organization(
                    "acme",
                    "admin",
                    &QueryContext::default(),
                    &"a".repeat(129)
                )
                .await
                .is_err()
        );
        allowed
            .update_organization(
                "acme",
                "admin",
                &QueryContext::default(),
                "Acme",
            )
            .await?;
        assert_eq!(store.writes.load(Ordering::SeqCst), 1);
        Ok(())
    }

    #[tokio::test]
    async fn passkey_administration_requires_tenant_admin_and_valid_targets()
    -> Result<()> {
        for decision in
            [AuthorizationDecision::Deny, AuthorizationDecision::Fail]
        {
            let (service, store, _, _) = service(decision, true);
            assert!(
                service
                    .set_passkey_mfa(
                        "acme",
                        "alice",
                        &QueryContext::default(),
                        true
                    )
                    .await
                    .is_err()
            );
            assert!(
                service
                    .reset_passkeys(
                        "acme",
                        "alice",
                        &QueryContext::default(),
                        "bob"
                    )
                    .await
                    .is_err()
            );
            assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        }
        let (service, store, _, _) =
            service(AuthorizationDecision::Allow, true);
        assert!(
            service
                .reset_passkeys(
                    "acme",
                    "admin",
                    &QueryContext::default(),
                    "../other"
                )
                .await
                .is_err()
        );
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        service
            .set_passkey_mfa("acme", "admin", &QueryContext::default(), true)
            .await?;
        service
            .reset_passkeys("acme", "admin", &QueryContext::default(), "bob")
            .await?;
        assert_eq!(store.writes.load(Ordering::SeqCst), 2);
        Ok(())
    }

    #[tokio::test]
    async fn rejected_admin_and_unverified_domain_have_no_broker_side_effects()
    -> Result<()> {
        for decision in
            [AuthorizationDecision::Deny, AuthorizationDecision::Fail]
        {
            let (service, store, broker, _) = service(decision, true);
            assert!(
                service
                    .configure(
                        "acme",
                        "alice",
                        &QueryContext::default(),
                        &connection()?
                    )
                    .await
                    .is_err()
            );
            assert!(
                service
                    .set_import(
                        "acme",
                        "alice",
                        &QueryContext::default(),
                        true
                    )
                    .await
                    .is_err()
            );
            assert_eq!(store.writes.load(Ordering::SeqCst), 0);
            assert_eq!(broker.configured.load(Ordering::SeqCst), 0);
            assert!(!store.import.load(Ordering::SeqCst));
        }
        let (service, store, broker, _) =
            service(AuthorizationDecision::Allow, false);
        assert!(
            service
                .configure(
                    "acme",
                    "alice",
                    &QueryContext::default(),
                    &connection()?
                )
                .await
                .is_err()
        );
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        assert_eq!(broker.configured.load(Ordering::SeqCst), 0);
        Ok(())
    }

    #[tokio::test]
    async fn permission_import_requires_opt_in_and_validates_all_policies_before_writes()
    -> Result<()> {
        let (service, _, _, authorization) =
            service(AuthorizationDecision::Allow, true);
        let manifest = serde_json::from_str(
            r#"{"version":1,"operations":[{"kind":"cedar_policy","id":"invalid","content":"not a policy","active":true}]}"#,
        )?;
        assert!(
            service
                .import("acme", "alice", &QueryContext::default(), &manifest)
                .await
                .is_err()
        );
        service
            .set_import("acme", "alice", &QueryContext::default(), true)
            .await?;
        assert!(
            service
                .import("acme", "alice", &QueryContext::default(), &manifest)
                .await
                .is_err()
        );
        assert!(
            authorization
                .mutations
                .lock()
                .map_err(|_| anyhow::anyhow!("Poisoned test lock"))?
                .is_empty()
        );
        Ok(())
    }

    #[tokio::test]
    async fn account_linking_is_an_explicit_authorized_operation() -> Result<()>
    {
        let (service, store, _, _) =
            service(AuthorizationDecision::Deny, true);
        assert!(
            service
                .link_identity(
                    "acme",
                    "admin",
                    &QueryContext::default(),
                    "acme.example",
                    "external",
                    "existing"
                )
                .await
                .is_err()
        );
        assert_eq!(store.writes.load(Ordering::SeqCst), 0);
        let (service, store, _, _) =
            self::service(AuthorizationDecision::Allow, true);
        service
            .link_identity(
                "acme",
                "admin",
                &QueryContext::default(),
                "acme.example",
                "external",
                "existing",
            )
            .await?;
        assert_eq!(store.writes.load(Ordering::SeqCst), 1);
        Ok(())
    }

    #[tokio::test]
    async fn compatible_imports_use_native_tenant_namespaces_and_stop_on_failure()
    -> Result<()> {
        let (service, _, _, authorization) =
            service(AuthorizationDecision::Allow, true);
        service
            .set_import("acme", "admin", &QueryContext::default(), true)
            .await?;
        let manifest = serde_json::from_str(
            r#"{"version":1,"operations":[{"kind":"relationship","resource":{"type":"group","id":"research"},"relation":"reader","subject":{"type":"user","id":"alice"},"enabled":true},{"kind":"cedar_policy","id":"deny-night","content":"forbid(principal, action, resource) when { context.hour_utc < 8 };","active":true}]}"#,
        )?;
        let result = service
            .import("acme", "admin", &QueryContext::default(), &manifest)
            .await?;
        assert!(result.complete);
        assert_eq!(result.applied, 2);
        {
            let writes = authorization
                .mutations
                .lock()
                .map_err(|_| anyhow::anyhow!("Poisoned test lock"))?;
            assert!(
                writes
                    .iter()
                    .any(|write| write.resource_id == "acme/research" &&
                        write.relation == "reader" &&
                        write.subject_id == "alice")
            );
        }
        let partial = serde_json::from_str(
            r#"{"version":1,"operations":[{"kind":"cedar_policy","id":"valid","content":"forbid(principal, action, resource) when { context.hour_utc < 8 };","active":true},{"kind":"role_assignment","user":"missing","role":"missing","enabled":true},{"kind":"relationship","resource":{"type":"group","id":"never-applied"},"relation":"reader","subject":{"type":"user","id":"alice"},"enabled":true}]}"#,
        )?;
        let result = service
            .import("acme", "admin", &QueryContext::default(), &partial)
            .await?;
        assert!(!result.complete);
        assert_eq!(result.applied, 1);
        assert_eq!(result.failed_at, Some(1));
        assert!(
            !authorization
                .mutations
                .lock()
                .map_err(|_| anyhow::anyhow!("Poisoned test lock"))?
                .iter()
                .any(|write| write.resource_id == "acme/never-applied")
        );
        Ok(())
    }

    #[tokio::test]
    async fn local_enrollment_uses_canonical_iam_without_a_polis_connection()
    -> Result<()> {
        let (mut service, store, broker, authorization) =
            service(AuthorizationDecision::Deny, true);
        let (audit_service, audit_store) = audit();
        service.audit = audit_service;
        let request = LocalProvisionRequest {
            request_id: Uuid::new_v4().to_string(),
            owner_external_subject: Uuid::new_v4().to_string(),
        };
        let organization = service
            .provision_local(&request, &QueryContext::default())
            .await?;
        assert!(organization.ready);
        {
            let events = audit_store
                .events
                .lock()
                .map_err(|_| anyhow::anyhow!("Poisoned audit test lock"))?;
            assert_eq!(events.len(), 2);
            assert!(
                events
                    .iter()
                    .all(|event| event.request_id == request.request_id)
            );
        }
        assert_eq!(store.writes.load(Ordering::SeqCst), 2);
        assert_eq!(broker.configured.load(Ordering::SeqCst), 0);
        assert_eq!(
            authorization
                .mutations
                .lock()
                .map_err(|_| anyhow::anyhow!("Poisoned test lock"))?
                .len(),
            1
        );
        let invalid = LocalProvisionRequest {
            request_id: Uuid::new_v4().to_string(),
            owner_external_subject: "email-is-not-an-identity".into(),
        };
        assert!(
            service
                .provision_local(&invalid, &QueryContext::default())
                .await
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn provisioning_creates_an_authorized_owner_before_publishing_discovery()
    -> Result<()> {
        let (service, store, broker, authorization) =
            service(AuthorizationDecision::Deny, true);
        let request = ProvisionRequest {
            request_id: Uuid::new_v4().to_string(),
            owner_external_subject: "immutable-sub".into(),
            connection: connection()?,
        };
        let organization = service
            .provision(&request, &QueryContext::default())
            .await?;
        assert_eq!(organization.tenant_id, "acme");
        assert_eq!(store.writes.load(Ordering::SeqCst), 4);
        assert_eq!(broker.configured.load(Ordering::SeqCst), 1);
        let mutations = authorization
            .mutations
            .lock()
            .map_err(|_| anyhow::anyhow!("Poisoned test lock"))?;
        assert_eq!(mutations.len(), 1);
        let owner = mutations
            .first()
            .ok_or_else(|| anyhow::anyhow!("Missing owner grant"))?;
        assert_eq!(
            (
                &*owner.resource_type,
                &*owner.resource_id,
                &*owner.relation,
                &*owner.subject_id
            ),
            ("tenant", "acme", "administrator", "owner")
        );
        Ok(())
    }
}
