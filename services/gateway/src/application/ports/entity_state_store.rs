//! Outbound port for reading entity states for search and graph hydration.

use anyhow::Result;
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntityStateRow {
    pub entity_id: String,
    pub metadata: Value,
    pub state_type: Option<String>,
    /// Created-at in unix milliseconds (UTC). Avoids formatting feature
    /// flags.
    pub created_at_ms: Option<i64>,
}

#[async_trait::async_trait]
pub trait EntityStateStore: Send + Sync {
    /// Searches current tenant state metadata by a bounded display name.
    async fn search_by_name(
        &self,
        tenant_id: &str,
        query: &str,
        limit: usize,
    ) -> Result<Vec<EntityStateRow>>;

    /// Loads bounded state history for one tenant entity.
    async fn latest_states_for_entity(
        &self,
        tenant_id: &str,
        entity_id: &str,
        limit: usize,
    ) -> Result<Vec<EntityStateRow>>;
}
