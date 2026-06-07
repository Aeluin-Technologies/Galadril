use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use mistralrs::tool;
use tokio::sync::watch;

type DbWatchChannel = (
    watch::Sender<Arc<dyn DatabaseProvider>>,
    watch::Receiver<Arc<dyn DatabaseProvider>>,
);

/// Trait that library consumers implement to provide data lookup capabilities
/// to the NLP model.
#[async_trait::async_trait]
pub trait DatabaseProvider: Send + Sync {
    /// Execute a lookup query requested by the model.
    #[allow(clippy::wrong_self_convention)]
    async fn from_database(&self, query: &str) -> Result<Option<String>>;
}

/// Default no-op provider. Always returns `None`.
pub struct NoOpProvider;

#[async_trait::async_trait]
impl DatabaseProvider for NoOpProvider {
    async fn from_database(&self, _query: &str) -> Result<Option<String>> {
        Ok(None)
    }
}

lazy_static::lazy_static! {
    /// Global async watch channel allowing lock-free reads across threads.
    static ref DB_WATCH: DbWatchChannel = {
        watch::channel(Arc::new(NoOpProvider) as Arc<dyn DatabaseProvider>)
    };
}

/// Sets the global database provider to be used by the agent.
pub fn set_database_provider(
    provider: impl DatabaseProvider + 'static,
) -> Result<()> {
    let provider_arc: Arc<dyn DatabaseProvider> = Arc::new(provider);
    DB_WATCH.0.send(provider_arc).map_err(|e| {
        anyhow!("Failed to broadcast new database provider across watch channel: {e}")
    })?;
    Ok(())
}

/// Query an external database to retrieve context data before writing.
#[tool(
    description = "Query an external GraphRAG database to retrieve verified facts, metrics, and structured data before writing. Use this strictly to ground your knowledge and avoid hallucinations."
)]
pub async fn from_database(
    #[description = "A precise natural-language query describing the specific data needed."]
    query: String,
) -> Result<String> {
    tracing::debug!(?query, "from_database tool invoked by agent");

    let provider = DB_WATCH.1.borrow().clone();

    provider
        .from_database(query.trim())
        .await
        .context("Database transaction encountered an unrecoverable operational failure")?
        .map_or_else(
            || Ok("No relevant data found in the database for your query. Do not hallucinate data; instead, note the absence of intelligence if applicable.".to_string()),
            Ok,
        )
}
