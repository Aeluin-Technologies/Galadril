//! Fake embedding generator used to unblock development.
//!
//! This adapter produces a deterministic 1024-d embedding where the first few
//! dimensions are derived from the input bytes and the rest are zeros.

use anyhow::Result;

use crate::application::ports::embedding_generator::{
    Embedding1024, EmbeddingGenerator,
};

/// Deterministic fake embedding generator.
#[derive(Debug, Default, Clone, Copy)]
pub struct FakeEmbeddingGenerator;

impl FakeEmbeddingGenerator {
    /// Creates a new [`FakeEmbeddingGenerator`].
    pub fn new() -> Self {
        Self
    }

    fn embed_deterministic(text: &str) -> Embedding1024 {
        let mut out = [0.0_f32; 1024];

        for (i, b) in text.as_bytes().iter().take(64).enumerate() {
            out[i] = *b as f32 / 255.0;
        }

        out
    }
}

#[async_trait::async_trait]
impl EmbeddingGenerator for FakeEmbeddingGenerator {
    async fn embed_text(&self, text: &str) -> Result<Embedding1024> {
        Ok(Self::embed_deterministic(text))
    }
}
