//! Boundary interfaces for dynamic external ingestion resources.

use anyhow::{Context, Result};
use async_trait::async_trait;

/// Driving endpoint for underlying background consumers.
#[async_trait]
pub trait IngestionServicePort: Send + Sync {
    /// Ingests storage records into broker streams.
    async fn process(&self, bucket: String, key: String) -> Result<()>;
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
        })
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
        };
        assert_eq!(h.viewers_csv(), None);
    }

    #[test]
    fn viewers_csv_trims_and_joins() {
        let h = AuthzHints {
            tenant: Some("t1".to_string()),
            viewers: vec![" user:a ".to_string(), "user:b".to_string()],
            owner: Some("u1".to_string()),
        };
        assert_eq!(h.viewers_csv().as_deref(), Some("user:a,user:b"));
    }

    #[test]
    fn require_tenant_owner_validates() {
        let ok = AuthzHints::require_tenant_owner(
            Some(" tenant:acme ".to_string()),
            Some(" user:alice ".to_string()),
            vec![],
        )
        .unwrap();
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
    }
}
