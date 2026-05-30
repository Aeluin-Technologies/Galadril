//! Galadril ports.

use anyhow::Result;
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
#[derive(Debug, Clone)]
pub struct AuthzHints {
    pub tenant: Option<String>,
    pub viewers: Vec<String>,
    pub owner: Option<String>,
}

// Driven Port for file storage.
#[async_trait]
pub trait BlobStorage: Send + Sync {
    async fn upload_file(
        &self,
        file_name: &str,
        data: &[u8],
    ) -> Result<String>;

    async fn download_file(&self, file_url: &str) -> Result<Vec<u8>>;

    /// Fetch authz-related hints from storage metadata/tags.
    async fn authz_hints(&self, bucket: &str, key: &str)
    -> Result<AuthzHints>;
}
