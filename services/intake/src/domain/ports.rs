//! Galadril ports.

use anyhow::{Context, Result};
use async_trait::async_trait;

// Driving Port for broker.
#[async_trait]
pub trait IngestionServicePort: Send + Sync {
    async fn process(&self, bucket: String, key: String) -> Result<()>;
}

// Driven Port for broker.
#[async_trait]
pub trait EventProducer: Send + Sync {
    /// Publish a dynamic payload.
    async fn publish(
        &self,
        topic: &str,
        schema_path: Option<&str>,
        key: &str,
        payload: &serde_json::Value,
    ) -> Result<()>;
}

/// Authz context hints extracted from storage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthzHints {
    pub tenant: Option<String>,
    pub viewers: Vec<String>,
    pub owner: Option<String>,
}

impl AuthzHints {
    /// Returns a comma-separated viewer list suitable for S3 metadata/tags.
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

    /// Returns a strict, tenant + owner enforced hint set.
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

// Driven Port for file storage.
#[async_trait]
pub trait BlobStorage: Send + Sync {
    async fn upload_file(
        &self,
        file_name: &str,
        data: &[u8],
    ) -> Result<String>;

    /// Uploads an object with authz metadata/tags.
    async fn upload_file_with_authz(
        &self,
        key: &str,
        data: &[u8],
        authz: &AuthzHints,
    ) -> Result<String>;

    async fn download_file(&self, file_url: &str) -> Result<Vec<u8>>;

    /// Fetch authz-related hints from storage metadata/tags.
    async fn authz_hints(&self, bucket: &str, key: &str)
    -> Result<AuthzHints>;
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
