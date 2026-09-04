//! Boundary interfaces for dynamic external ingestion resources.

use anyhow::{Context, Result, ensure};
use async_trait::async_trait;

use crate::domain::models::FileEvent;

/// Kafka header carrying the trusted tenant capability.
pub const HEADER_TENANT_ID: &str = "galadril-tenant-id";
/// Kafka header carrying the immutable pipeline identifier.
pub const HEADER_PIPELINE_ID: &str = "galadril-pipeline-id";
/// Kafka header carrying the immutable TerminusDB revision.
pub const HEADER_PIPELINE_REVISION: &str = "galadril-pipeline-revision";

/// Rejects ambiguous tenant identifiers before touching a shared connection.
pub fn validate_pipeline_tenant(tenant: &str) -> Result<()> {
    ensure!(
        !tenant.is_empty() &&
            tenant.len() <= 64 &&
            tenant.bytes().all(|b| b.is_ascii_alphanumeric() ||
                b == b'_' ||
                b == b'-'),
        "Invalid pipeline tenant"
    );
    Ok(())
}

fn validate_identity_segment(value: &str, label: &str) -> Result<()> {
    ensure!(
        !value.is_empty() &&
            value.len() <= 128 &&
            value.bytes().all(|byte| byte.is_ascii_alphanumeric() ||
                byte == b'_' ||
                byte == b'-'),
        "Invalid {label}"
    );
    Ok(())
}

/// Immutable execution identity selected from a trusted publication.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PipelineIdentity {
    /// Tenant capability used to read the published snapshot.
    pub tenant_id: String,
    /// Stable logical pipeline identifier.
    pub pipeline_id: String,
    /// Immutable TerminusDB commit identifier.
    pub revision_id: String,
}

impl PipelineIdentity {
    /// Validates all identity components before they cross a service boundary.
    pub fn new(
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<Self> {
        validate_pipeline_tenant(tenant_id)?;
        validate_identity_segment(pipeline_id, "pipeline identifier")?;
        validate_identity_segment(revision_id, "pipeline revision")?;
        Ok(Self {
            tenant_id: tenant_id.to_owned(),
            pipeline_id: pipeline_id.to_owned(),
            revision_id: revision_id.to_owned(),
        })
    }

    /// Returns the canonical identity consumed by Vision command routing.
    pub fn execution_identity(&self) -> String {
        format!(
            "{}/{}/{}",
            self.tenant_id, self.pipeline_id, self.revision_id
        )
    }

    /// Borrows the fixed Kafka identity header set without cloning values.
    pub fn header_entries(&self) -> [(&'static str, &str); 3] {
        [
            (HEADER_TENANT_ID, self.tenant_id.as_str()),
            (HEADER_PIPELINE_ID, self.pipeline_id.as_str()),
            (HEADER_PIPELINE_REVISION, self.revision_id.as_str()),
        ]
    }
}

/// One published pipeline definition pinned to an immutable revision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishedPipeline {
    /// Trusted publication coordinates.
    pub identity: PipelineIdentity,
    /// Serialized pipeline definition from the pinned commit.
    pub definition: String,
}

/// Published definitions are resolved exclusively within a trusted tenant.
#[async_trait]
pub trait PipelineCatalog: Send + Sync {
    /// Rejects tenants absent from the process's trusted capability map.
    fn authorize_tenant(&self, tenant_id: &str) -> Result<()>;

    /// Returns immutable published JSON definitions, excluding deleted drafts.
    async fn published(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<PublishedPipeline>>;
}

/// Driving endpoint for underlying background consumers.
#[async_trait]
pub trait IngestionServicePort: Send + Sync {
    /// Ingests storage records into broker streams.
    async fn process(&self, event: FileEvent) -> Result<()>;
}

/// Driven messaging port.
#[async_trait]
pub trait EventProducer: Send + Sync {
    /// Publishes raw/structured records.
    async fn publish(
        &self,
        topic: &str,
        schema_path: Option<&str>,
        key: &str,
        payload: &serde_json::Value,
        identity: &PipelineIdentity,
    ) -> Result<()>;
}

/// Multi-tenant tenancy boundaries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthzHints {
    /// Associated namespace.
    pub tenant: Option<String>,
    /// Authorized readers.
    pub viewers: Vec<String>,
    /// Context resource identity.
    pub owner: Option<String>,
    /// Trusted service that established this metadata.
    pub source_principal: Option<String>,
    /// Scoped permission delegated to Intake.
    pub permission: Option<String>,
    /// Tenant-qualified resource bound by the delegation.
    pub resource: Option<String>,
    /// Signed authentication issuer recorded by Gateway.
    pub authentication_provenance: Option<String>,
    /// Stable scoped delegation identifier for cross-service lineage.
    pub delegation_id: Option<String>,
}

impl AuthzHints {
    /// Maps internal viewers collection into metadata-safe strings.
    pub fn viewers_csv(&self) -> Option<String> {
        let mut out = String::new();
        for v in &self.viewers {
            let v = v.trim();
            if v.is_empty() {
                continue;
            }
            if !out.is_empty() {
                out.push(',');
            }
            out.push_str(v);
        }
        if out.is_empty() { None } else { Some(out) }
    }

    /// Asserts critical tenancy constraints on creation.
    pub fn require_tenant_owner(
        tenant: Option<String>,
        owner: Option<String>,
        viewers: Vec<String>,
    ) -> Result<Self> {
        let tenant = tenant
            .map(|t| t.trim().to_string())
            .filter(|t| !t.is_empty())
            .context("authz tenant is required")?;
        let owner = owner
            .map(|o| o.trim().to_string())
            .filter(|o| !o.is_empty())
            .context("authz owner is required")?;

        Ok(Self {
            tenant: Some(tenant),
            viewers,
            owner: Some(owner),
            source_principal: Some("service:gateway".to_string()),
            permission: Some("ingest".to_string()),
            resource: None,
            authentication_provenance: None,
            delegation_id: None,
        })
    }

    /// Validates the infrastructure-authenticated Gateway delegation.
    pub fn require_trusted_ingestion(&self, object_key: &str) -> Result<&str> {
        let tenant = self
            .tenant
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("trusted authz tenant is required")?;
        self.owner
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("trusted authz owner is required")?;
        if self.source_principal.as_deref() != Some("service:gateway") {
            anyhow::bail!("object was not authorized by Gateway");
        }
        if self.permission.as_deref() != Some("ingest") {
            anyhow::bail!("object delegation does not permit ingestion");
        }
        let expected_resource =
            format!("raw:{}", object_key.trim_start_matches('/'));
        if self.resource.as_deref() != Some(expected_resource.as_str()) {
            anyhow::bail!("object delegation resource mismatch");
        }
        self.authentication_provenance
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("authentication provenance is required")?;
        self.delegation_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("delegation identifier is required")?;
        let path_tenant = object_key
            .trim_start_matches('/')
            .split('/')
            .next()
            .context("object key is missing tenant prefix")?;
        if path_tenant != tenant {
            anyhow::bail!("object delegation tenant mismatch");
        }
        Ok(tenant)
    }
}

/// Driven port for remote binary interactions.
#[async_trait]
pub trait BlobStorage: Send + Sync {
    /// Pushes atomic binary blocks.
    async fn upload_file(
        &self,
        file_name: &str,
        data: &[u8],
    ) -> Result<String>;

    /// Transmits data blocks alongside explicit multi-tenant constraints.
    async fn upload_file_with_authz(
        &self,
        key: &str,
        data: &[u8],
        authz: &AuthzHints,
    ) -> Result<String>;

    /// Pulls atomic payload references.
    async fn download_file(&self, file_url: &str) -> Result<Vec<u8>>;

    /// Queries associated ownership and reader tags.
    async fn authz_hints(&self, bucket: &str, key: &str)
    -> Result<AuthzHints>;

    /// Lists object keys under a given storage prefix.
    async fn list_objects(&self, prefix: &str) -> Result<Vec<(String, i64)>>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn viewers_csv_none_when_empty() {
        let h = AuthzHints {
            tenant: Some("t1".to_string()),
            viewers: vec![],
            owner: Some("u1".to_string()),
            source_principal: None,
            permission: None,
            resource: None,
            authentication_provenance: None,
            delegation_id: None,
        };
        assert_eq!(h.viewers_csv(), None);
    }

    #[test]
    fn viewers_csv_trims_and_joins() {
        let h = AuthzHints {
            tenant: Some("t1".to_string()),
            viewers: vec![" user:a ".to_string(), "user:b".to_string()],
            owner: Some("u1".to_string()),
            source_principal: None,
            permission: None,
            resource: None,
            authentication_provenance: None,
            delegation_id: None,
        };
        assert_eq!(h.viewers_csv().as_deref(), Some("user:a,user:b"));
    }

    #[test]
    fn require_tenant_owner_validates() -> Result<()> {
        let ok = AuthzHints::require_tenant_owner(
            Some(" tenant:acme ".to_string()),
            Some(" user:alice ".to_string()),
            vec![],
        )?;
        assert_eq!(ok.tenant.as_deref(), Some("tenant:acme"));
        assert_eq!(ok.owner.as_deref(), Some("user:alice"));

        assert!(
            AuthzHints::require_tenant_owner(None, Some("x".into()), vec![])
                .is_err()
        );
        assert!(
            AuthzHints::require_tenant_owner(Some("x".into()), None, vec![])
                .is_err()
        );
        assert!(
            AuthzHints::require_tenant_owner(
                Some(" ".into()),
                Some("x".into()),
                vec![]
            )
            .is_err()
        );
        Ok(())
    }

    #[test]
    fn trusted_ingestion_binds_tenant_resource_and_provenance() {
        let trusted = AuthzHints {
            tenant: Some("tenant-a".to_owned()),
            viewers: Vec::new(),
            owner: Some("user-a".to_owned()),
            source_principal: Some("service:gateway".to_owned()),
            permission: Some("ingest".to_owned()),
            resource: Some("raw:tenant-a/topic/object".to_owned()),
            authentication_provenance: Some(
                "https://issuer.example".to_owned(),
            ),
            delegation_id: Some("delegation-1".to_owned()),
        };
        assert_eq!(
            trusted
                .require_trusted_ingestion("tenant-a/topic/object")
                .ok(),
            Some("tenant-a")
        );
        assert!(
            trusted
                .require_trusted_ingestion("tenant-b/topic/object")
                .is_err()
        );

        let mut missing_lineage = trusted;
        missing_lineage.delegation_id = None;
        assert!(
            missing_lineage
                .require_trusted_ingestion("tenant-a/topic/object")
                .is_err()
        );
    }

    #[test]
    fn trusted_ingestion_rejects_each_missing_or_forged_field() {
        let trusted = AuthzHints {
            tenant: Some("tenant-a".to_owned()),
            viewers: Vec::new(),
            owner: Some("user-a".to_owned()),
            source_principal: Some("service:gateway".to_owned()),
            permission: Some("ingest".to_owned()),
            resource: Some("raw:tenant-a/topic/object".to_owned()),
            authentication_provenance: Some(
                "https://issuer.example".to_owned(),
            ),
            delegation_id: Some("delegation-1".to_owned()),
        };
        let mut invalid = Vec::with_capacity(8);

        let mut case = trusted.clone();
        case.tenant = None;
        invalid.push(case);
        let mut case = trusted.clone();
        case.owner = None;
        invalid.push(case);
        let mut case = trusted.clone();
        case.source_principal = Some("service:attacker".to_owned());
        invalid.push(case);
        let mut case = trusted.clone();
        case.permission = Some("view".to_owned());
        invalid.push(case);
        let mut case = trusted.clone();
        case.resource = Some("raw:tenant-b/topic/object".to_owned());
        invalid.push(case);
        let mut case = trusted.clone();
        case.authentication_provenance = Some(String::new());
        invalid.push(case);
        let mut case = trusted.clone();
        case.delegation_id = Some(String::new());
        invalid.push(case);
        let mut case = trusted;
        case.tenant = Some("tenant-b".to_owned());
        invalid.push(case);

        for hints in invalid {
            assert!(
                hints
                    .require_trusted_ingestion("tenant-a/topic/object")
                    .is_err()
            );
        }
    }

    #[test]
    fn pipeline_identity_exposes_complete_immutable_kafka_headers()
    -> Result<()> {
        let identity =
            PipelineIdentity::new("tenant-a", "daily", "revision-1")?;

        assert_eq!(
            identity.header_entries(),
            [
                (HEADER_TENANT_ID, "tenant-a"),
                (HEADER_PIPELINE_ID, "daily"),
                (HEADER_PIPELINE_REVISION, "revision-1"),
            ]
        );
        assert_eq!(identity.execution_identity(), "tenant-a/daily/revision-1");
        Ok(())
    }
}
