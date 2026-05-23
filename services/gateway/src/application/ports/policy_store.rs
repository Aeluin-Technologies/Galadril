//! Outbound port for Cedar policy retrieval.

use anyhow::Result;

use crate::domain::policy::PolicyRecord;

#[async_trait::async_trait]
pub trait PolicyStore: Send + Sync {
    /// Retrieves all active Cedar policies for a given tenant from the store.
    async fn get_active_policies(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<PolicyRecord>>;
}
