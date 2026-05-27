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
    async fn search_events(
        &self,
        tenant_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
    ) -> Result<Vec<EventRow>>;

    async fn search_embeddings_top_k(
        &self,
        tenant_id: &str,
        modality: Option<&str>,
        embedding: &[f32; 1024],
        k: usize,
    ) -> Result<Vec<EmbeddingRow>>;
}
