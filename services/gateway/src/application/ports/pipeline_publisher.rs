//! Runtime publication contract for tenant pipeline configurations.

use anyhow::Result;
use serde_json::Value;

/// Publishes authorized pipeline definitions to the runtime configuration
/// store.
#[async_trait::async_trait]
pub trait PipelinePublisher: Send + Sync {
    /// Replaces one tenant pipeline's active configuration object.
    async fn publish(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        revision_id: &str,
        definition: &Value,
    ) -> Result<()>;

    /// Removes one tenant pipeline from runtime discovery.
    async fn retire(&self, tenant_id: &str, pipeline_id: &str) -> Result<()>;
}
