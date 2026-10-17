//! Outbound port for cross-domain search (states, events, embeddings).

use anyhow::Result;
use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct EventRow {
    pub event_id: String,
    pub event_type: String,
    pub event_time_ms: i64,
    pub properties: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct EmbeddingRow {
    pub id: Option<String>,
    pub entity_id: String,
    pub modality: String,
    pub created_at_ms: i64,
    pub metadata: Value,
    /// Distance/similarity score (lower is closer using L2 distance).
    pub score: f32,
}

#[async_trait::async_trait]
pub trait SearchStore: Send + Sync {
    /// Adapters must implement continuation explicitly rather than repeat page
    /// one.
    async fn search_events_page(
        &self,
        tenant_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
        offset: u32,
    ) -> Result<Vec<EventRow>> {
        anyhow::ensure!(offset == 0, "Event pagination is unavailable");
        self.search_events(tenant_id, event_type, text, limit).await
    }

    /// Loads one bounded continuation of nearest tenant embeddings.
    async fn search_embeddings_page(
        &self,
        tenant_id: &str,
        modality: Option<&str>,
        embedding: &[f32; 1024],
        limit: usize,
        offset: u32,
    ) -> Result<Vec<EmbeddingRow>> {
        anyhow::ensure!(offset == 0, "Embedding pagination is unavailable");
        self.search_embeddings_top_k(tenant_id, modality, embedding, limit)
            .await
    }

    /// Searches tenant event evidence using structured optional constraints.
    async fn search_events(
        &self,
        tenant_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
    ) -> Result<Vec<EventRow>>;

    /// Finds bounded tenant embeddings nearest to a precomputed vector.
    async fn search_embeddings_top_k(
        &self,
        tenant_id: &str,
        modality: Option<&str>,
        embedding: &[f32; 1024],
        k: usize,
    ) -> Result<Vec<EmbeddingRow>>;
}
