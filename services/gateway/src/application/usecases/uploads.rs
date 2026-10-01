//! Authorized and durably audited tenant upload orchestration.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use uuid::Uuid;

use crate::application::ports::upload_store::{
    UploadFinalization, UploadStore,
};
use crate::application::usecases::audit::{
    AuditAction, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;
use crate::domain::key::{sanitize_upload_group, sanitize_upload_request};

const UPLOAD_URL_TTL: Duration = Duration::from_secs(15 * 60);

/// Stable direct-upload coordinates returned to a GraphQL client.
pub struct StagingUpload {
    pub upload_url: String,
    pub staging_key: String,
}

/// A staged capability and its explicitly scoped production destination.
pub struct UploadDestination<'a> {
    pub staging_key: &'a str,
    pub target_name: &'a str,
    pub group_id: Option<&'a str>,
}

/// Coordinates upload authorization, storage promotion, SpiceDB, and audit.
pub struct UploadService {
    store: Arc<dyn UploadStore>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
}

impl UploadService {
    /// Creates an upload service from reusable security and storage ports.
    pub fn new(
        store: Arc<dyn UploadStore>,
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

    /// Persists an attempt before applying identity, SpiceDB, and Cedar.
    async fn begin_authorized(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        action: AuditAction,
        resource_id: &str,
        group_id: &str,
    ) -> Result<crate::application::usecases::audit::AuditOperation> {
        let operation = self
            .audit
            .begin(
                tenant_id,
                user_id,
                AuditTarget::new(action, "raw", resource_id),
                context,
            )
            .await?;
        if let Err(error) = self.identity.verify_user(tenant_id, user_id).await
        {
            operation.denied("identity_denied").await?;
            return Err(error);
        }
        let tenant_decision = self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                Permission::Ingest,
                "tenant",
                tenant_id,
                Some(context),
            )
            .await;
        let decision = match tenant_decision {
            Ok(true) => Ok(true),
            Ok(false) => {
                self.auth
                    .is_authorized(
                        user_id,
                        tenant_id,
                        Permission::Ingest,
                        "group",
                        &format!("{tenant_id}/{group_id}"),
                        Some(context),
                    )
                    .await
            },
            Err(error) => Err(error),
        };
        match decision {
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

    /// Creates a short-lived staging URL scoped to one tenant and principal.
    pub async fn request_staging_upload(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        group_id: Option<&str>,
    ) -> Result<StagingUpload> {
        let group_id = sanitize_upload_group(group_id)?;
        let staging_key = format!(
            "{tenant_id}/{}/{group_id}/{}",
            urlencoding::encode(user_id),
            Uuid::new_v4().simple()
        );
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditAction::RequestStagingUpload,
                &staging_key,
                &group_id,
            )
            .await?;
        let upload_url = match self
            .store
            .presign_upload(&staging_key, UPLOAD_URL_TTL)
            .await
        {
            Ok(url) => url,
            Err(error) => {
                operation.failed("storage_presign_failed").await?;
                return Err(error);
            },
        };
        operation.succeeded().await?;
        Ok(StagingUpload {
            upload_url,
            staging_key,
        })
    }

    /// Promotes an upload and establishes raw-resource ownership.
    pub async fn complete_upload(
        &self,
        tenant_id: &str,
        user_id: &str,
        authn_issuer: &str,
        context: &QueryContext,
        destination: UploadDestination<'_>,
    ) -> Result<String> {
        let upload = sanitize_upload_request(
            tenant_id,
            destination.group_id,
            destination.target_name,
        )
        .context("Invalid upload parameters")?;
        let operation = self
            .begin_authorized(
                tenant_id,
                user_id,
                context,
                AuditAction::CompleteUpload,
                &upload.s3_key,
                &upload.group_id,
            )
            .await?;
        let mut staging_parts = destination.staging_key.split('/');
        let valid_staging_scope = matches!(
            (
                staging_parts.next(),
                staging_parts.next(),
                staging_parts.next(),
                staging_parts.next(),
                staging_parts.next(),
            ),
            (Some(tenant), Some(owner), Some(group), Some(nonce), None)
                if tenant == tenant_id &&
                    owner == urlencoding::encode(user_id) &&
                    group == upload.group_id &&
                    nonce.len() == 32 &&
                    nonce.bytes().all(|byte| byte.is_ascii_hexdigit())
        );
        if !valid_staging_scope {
            operation.denied("staging_scope_mismatch").await?;
            bail!("Staging upload scope does not match destination");
        }
        let authn_issuer = authn_issuer.trim();
        if authn_issuer.is_empty() {
            operation
                .failed("authentication_provenance_missing")
                .await?;
            bail!("Authentication provenance is missing");
        }
        let delegation_id = Uuid::new_v4().simple().to_string();
        let tagging_query = Self::tagging_query(tenant_id, user_id);
        let destination_key = match self
            .store
            .finalize_upload(UploadFinalization {
                staging_key: destination.staging_key,
                destination_key: &upload.s3_key,
                tenant_id,
                user_id,
                authn_issuer,
                delegation_id: &delegation_id,
                tagging_query: Some(&tagging_query),
            })
            .await
        {
            Ok(key) => key,
            Err(error) => {
                operation.failed("storage_promotion_failed").await?;
                return Err(error);
            },
        };
        let domain_id = format!("{tenant_id}/{}", upload.group_id);
        for (relation, subject_type, subject_id) in [
            ("parent", "tenant", tenant_id),
            ("domain", "group", domain_id.as_str()),
            ("owner", "user", user_id),
        ] {
            if let Err(error) = self
                .auth
                .upsert_relationship(
                    "raw",
                    &destination_key,
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
        operation.succeeded().await?;
        tracing::info!(
            event.name = "security.context.issued",
            tenant_id,
            actor_id = user_id,
            delegation_id,
            permission = "ingest",
            resource_type = "raw",
            resource_id = destination_key,
            service = "gateway",
            "issued scoped ingestion delegation"
        );
        Ok(destination_key)
    }

    /// Builds URL-encoded immutable ownership tags for an ingested object.
    fn tagging_query(tenant_id: &str, owner_id: &str) -> String {
        format!(
            "tenant={}&owner={}",
            urlencoding::encode(tenant_id.trim()),
            urlencoding::encode(owner_id.trim())
        )
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

    struct ScopedAuthorization;

    #[async_trait::async_trait]
    impl Authorization for ScopedAuthorization {
        async fn upsert_relationship(
            &self,
            _resource_type: &str,
            _resource_id: &str,
            _relation: &str,
            _subject_type: &str,
            _subject_id: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn delete_relationship(
            &self,
            _resource_type: &str,
            _resource_id: &str,
            _relation: &str,
            _subject_type: &str,
            _subject_id: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn is_authorized(
            &self,
            _user_id: &str,
            _tenant_id: &str,
            permission: Permission,
            resource_type: &str,
            resource_id: &str,
            _context: Option<&QueryContext>,
        ) -> Result<bool> {
            Ok(permission == Permission::Ingest &&
                resource_type == "group" &&
                resource_id == "tenant_a/sales")
        }

        async fn invalidate_tenant_cache(&self, _tenant_id: &str) {}
    }

    #[derive(Debug, Clone, PartialEq, Eq)]
    struct FinalizationRecord {
        staging_key: String,
        destination_key: String,
        tenant_id: String,
        user_id: String,
        authn_issuer: String,
        delegation_id: String,
        tagging_query: Option<String>,
    }

    #[derive(Default)]
    struct MemoryUploadStore {
        presigned_keys: Mutex<Vec<String>>,
        finalizations: Mutex<Vec<FinalizationRecord>>,
    }

    #[async_trait::async_trait]
    impl UploadStore for MemoryUploadStore {
        async fn presign_upload(
            &self,
            key: &str,
            _expires_in: Duration,
        ) -> Result<String> {
            self.presigned_keys
                .lock()
                .map_err(|error| {
                    anyhow!("upload test lock poisoned: {error}")
                })?
                .push(key.to_owned());
            Ok(format!("https://s3/{key}"))
        }

        async fn finalize_upload(
            &self,
            request: UploadFinalization<'_>,
        ) -> Result<String> {
            self.finalizations
                .lock()
                .map_err(|error| {
                    anyhow!("upload test lock poisoned: {error}")
                })?
                .push(FinalizationRecord {
                    staging_key: request.staging_key.to_owned(),
                    destination_key: request.destination_key.to_owned(),
                    tenant_id: request.tenant_id.to_owned(),
                    user_id: request.user_id.to_owned(),
                    authn_issuer: request.authn_issuer.to_owned(),
                    delegation_id: request.delegation_id.to_owned(),
                    tagging_query: request.tagging_query.map(str::to_owned),
                });
            Ok(request.destination_key.to_owned())
        }
    }

    #[tokio::test]
    async fn upload_lifecycle_is_authorized_owned_and_durably_audited()
    -> Result<()> {
        let store = Arc::new(MemoryUploadStore::default());
        let authorization =
            TestAuthorization::new(AuthorizationDecision::Allow);
        let (audit, audit_store) = audit();
        let service = UploadService::new(
            store.clone(),
            identity(true),
            authorization.clone(),
            audit,
        );
        let context = QueryContext {
            request_id: "request-upload".to_owned(),
            ..QueryContext::default()
        };
        let staging = service
            .request_staging_upload("tenant_a", "user_a", &context, None)
            .await?;
        ensure!(staging.staging_key.starts_with("tenant_a/user_a/"));
        ensure!(staging.upload_url.ends_with(&staging.staging_key));
        ensure!(
            service
                .complete_upload(
                    "tenant_a",
                    "user_a",
                    "https://issuer.example",
                    &context,
                    UploadDestination {
                        staging_key: &staging.staging_key,
                        target_name: "image.png",
                        group_id: Some("sales"),
                    },
                )
                .await
                .is_err()
        );
        ensure!(
            store
                .finalizations
                .lock()
                .map_err(|error| anyhow!(
                    "upload test lock poisoned: {error}"
                ))?
                .is_empty()
        );
        let destination = service
            .complete_upload(
                "tenant_a",
                "user_a",
                "https://issuer.example",
                &context,
                UploadDestination {
                    staging_key: &staging.staging_key,
                    target_name: "image.png",
                    group_id: None,
                },
            )
            .await?;
        ensure!(destination == "tenant_a/raw/default/image.png");
        let finalizations = store
            .finalizations
            .lock()
            .map_err(|error| anyhow!("upload test lock poisoned: {error}"))?;
        let finalization =
            finalizations.first().context("missing finalization")?;
        ensure!(finalization.tenant_id == "tenant_a");
        ensure!(finalization.user_id == "user_a");
        ensure!(finalization.authn_issuer == "https://issuer.example");
        ensure!(
            finalization.tagging_query.as_deref() ==
                Some("tenant=tenant_a&owner=user_a")
        );
        ensure!(finalization.delegation_id.len() == 32);
        drop(finalizations);
        let mutations = authorization.mutations.lock().map_err(|error| {
            anyhow!("authorization test lock poisoned: {error}")
        })?;
        ensure!(mutations.len() == 3);
        ensure!(mutations.iter().all(|mutation| {
            mutation.resource_type == "raw" &&
                mutation.resource_id == "tenant_a/raw/default/image.png"
        }));
        ensure!(mutations.iter().any(|mutation| {
            mutation.relation == "domain" &&
                mutation.subject_type == "group" &&
                mutation.subject_id == "tenant_a/default"
        }));
        drop(mutations);
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 6);
        ensure!(
            events
                .iter()
                .all(|event| event.request_id == "request-upload")
        );
        Ok(())
    }

    #[tokio::test]
    async fn denied_upload_never_reaches_storage() -> Result<()> {
        let store = Arc::new(MemoryUploadStore::default());
        let (audit, audit_store) = audit();
        let service = UploadService::new(
            store.clone(),
            identity(true),
            TestAuthorization::new(AuthorizationDecision::Deny),
            audit,
        );
        ensure!(
            service
                .request_staging_upload(
                    "tenant_a",
                    "user_a",
                    &QueryContext::default(),
                    None,
                )
                .await
                .is_err()
        );
        ensure!(
            store
                .presigned_keys
                .lock()
                .map_err(|error| anyhow!(
                    "upload test lock poisoned: {error}"
                ))?
                .is_empty()
        );
        let events = audit_store
            .events
            .lock()
            .map_err(|error| anyhow!("audit test lock poisoned: {error}"))?;
        ensure!(events.len() == 2);
        ensure!(events.last().map(|event| event.outcome) == Some(crate::application::ports::audit_store::AuditOutcome::Denied));
        Ok(())
    }

    #[tokio::test]
    async fn delegated_upload_cannot_cross_its_authorized_group() -> Result<()>
    {
        let store = Arc::new(MemoryUploadStore::default());
        let (audit, _) = audit();
        let service = UploadService::new(
            store.clone(),
            identity(true),
            Arc::new(ScopedAuthorization),
            audit,
        );
        let context = QueryContext::default();
        ensure!(
            service
                .request_staging_upload(
                    "tenant_a",
                    "steward",
                    &context,
                    Some("returns"),
                )
                .await
                .is_err()
        );
        let staging = service
            .request_staging_upload(
                "tenant_a",
                "steward",
                &context,
                Some("sales"),
            )
            .await?;
        ensure!(staging.staging_key.starts_with("tenant_a/steward/sales/"));
        ensure!(
            service
                .complete_upload(
                    "tenant_a",
                    "steward",
                    "https://issuer.example",
                    &context,
                    UploadDestination {
                        staging_key: &staging.staging_key,
                        target_name: "record.csv",
                        group_id: Some("returns"),
                    },
                )
                .await
                .is_err()
        );
        ensure!(
            store
                .finalizations
                .lock()
                .map_err(|error| anyhow!(
                    "upload test lock poisoned: {error}"
                ))?
                .is_empty()
        );
        let destination = service
            .complete_upload(
                "tenant_a",
                "steward",
                "https://issuer.example",
                &context,
                UploadDestination {
                    staging_key: &staging.staging_key,
                    target_name: "record.csv",
                    group_id: Some("sales"),
                },
            )
            .await?;
        ensure!(destination == "tenant_a/raw/sales/record.csv");
        Ok(())
    }
}
