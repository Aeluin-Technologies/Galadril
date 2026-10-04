//! Tenant-scoped access to persisted Amarth results.

use anyhow::Result;
use serde_json::Value;

pub struct CausalRun {
    pub cache_key: String,
    pub summary: Value,
}

#[async_trait::async_trait]
pub trait CausalStore: Send + Sync {
    async fn latest_for_entity(
        &self,
        tenant_id: &str,
        entity_id: &str,
    ) -> Result<Option<CausalRun>>;
}
