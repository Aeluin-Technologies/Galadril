//! Outbound port for turning a text query into an embedding vector.

use anyhow::Result;

/// A fixed-size embedding vector used for similarity search.
pub type Embedding1024 = [f32; 1024];

#[async_trait::async_trait]
pub trait EmbeddingGenerator: Send + Sync {
    /// Produces an embedding for the given text query.
    async fn embed_text(&self, text: &str) -> Result<Embedding1024>;
}
