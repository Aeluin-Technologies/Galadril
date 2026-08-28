//! Structured database retrieval tool for grounded Scribe responses.

use std::future::Future;
use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use mistralrs::tool;
use serde::{Deserialize, Serialize};

const DEFAULT_RESULT_LIMIT: u16 = 10;
const MAX_RESULT_LIMIT: u16 = 50;
const NO_DATA_GUIDANCE: &str = "No verified data matched the request. State that the data is unavailable; do not invent facts.";

/// Search intent passed to the database integration layer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DatabaseQuery {
    /// One self-contained factual question expressed without SQL syntax.
    pub question: String,
    /// Optional entity names or domain terms that improve retrieval recall.
    pub keywords: Vec<String>,
    /// Optional tenant-safe business or temporal scope.
    pub scope: Option<String>,
    /// Maximum number of evidence records requested from the provider.
    pub max_results: u16,
}

impl DatabaseQuery {
    fn new(
        question: String,
        mut keywords: Vec<String>,
        scope: Option<String>,
        max_results: Option<u16>,
    ) -> Result<Self> {
        let question = question.trim().to_owned();
        if question.is_empty() {
            return Err(anyhow!("database question must not be empty"));
        }

        keywords.iter_mut().for_each(|keyword| {
            keyword.truncate(keyword.trim_end().len());
            let leading = keyword.len() - keyword.trim_start().len();
            keyword.drain(..leading);
        });
        keywords.retain(|keyword| !keyword.is_empty());
        keywords.sort_unstable();
        keywords.dedup();

        let scope = scope
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty());
        let max_results = max_results
            .unwrap_or(DEFAULT_RESULT_LIMIT)
            .clamp(1, MAX_RESULT_LIMIT);

        Ok(Self {
            question,
            keywords,
            scope,
            max_results,
        })
    }
}

/// Trait implemented by the retrieval layer that grounds model responses.
#[async_trait::async_trait]
pub trait DatabaseProvider: Send + Sync {
    /// Retrieves verified evidence for a structured model query.
    async fn query_database(
        &self,
        query: &DatabaseQuery,
    ) -> Result<Option<String>>;
}

/// Default provider used when no retrieval integration is configured.
pub struct NoOpProvider;

#[async_trait::async_trait]
impl DatabaseProvider for NoOpProvider {
    async fn query_database(
        &self,
        _query: &DatabaseQuery,
    ) -> Result<Option<String>> {
        Ok(None)
    }
}

tokio::task_local! {
    static DATABASE_PROVIDER: Arc<dyn DatabaseProvider>;
}

/// Binds one retrieval provider to the current asynchronous generation only.
pub async fn with_database_provider<T>(
    provider: Arc<dyn DatabaseProvider>,
    future: impl Future<Output = T>,
) -> T {
    DATABASE_PROVIDER.scope(provider, future).await
}

#[derive(Serialize)]
struct DatabaseToolResponse<'a> {
    status: &'static str,
    question: &'a str,
    evidence: Option<&'a str>,
    guidance: &'static str,
}

/// Retrieves verified evidence using model-friendly natural-language fields.
#[tool(
    description = "Search the external GraphRAG database for verified evidence. Ask one precise factual question per call in natural language, never SQL. Add short keywords for named entities and an optional scope for a time period or business domain."
)]
#[tracing::instrument(
    name = "scribe.tool.database",
    skip(question, keywords, scope),
    fields(result_limit = max_results.unwrap_or(DEFAULT_RESULT_LIMIT))
)]
pub async fn query_database(
    #[description = "One precise, self-contained factual question. Do not write SQL."]
    question: String,
    #[description = "Optional short entity names or domain terms, such as product IDs, locations, or metric names."]
    keywords: Option<Vec<String>>,
    #[description = "Optional time period or business scope needed to disambiguate the question."]
    scope: Option<String>,
    #[description = "Maximum evidence records to retrieve; use a small value unless broader comparison is required."]
    max_results: Option<u16>,
) -> Result<String> {
    let query = DatabaseQuery::new(
        question,
        keywords.unwrap_or_default(),
        scope,
        max_results,
    )?;
    tracing::debug!(
        event.name = "scribe.tool.database.started",
        result_limit = query.max_results,
        "database retrieval tool started"
    );

    let provider = DATABASE_PROVIDER
        .try_with(Arc::clone)
        .context("database retrieval requires a request-scoped provider")?;
    let evidence = match provider.query_database(&query).await {
        Ok(evidence) => evidence,
        Err(error) => {
            tracing::error!(
                event.name = "scribe.tool.database.failed",
                error = %error,
                "database retrieval tool failed"
            );
            return Err(error).context("database retrieval failed");
        },
    };

    let response = DatabaseToolResponse {
        status: if evidence.is_some() {
            "found"
        } else {
            "not_found"
        },
        question: &query.question,
        evidence: evidence.as_deref(),
        guidance: if evidence.is_some() {
            "Use only the returned evidence for factual claims."
        } else {
            NO_DATA_GUIDANCE
        },
    };
    let serialized = serde_json::to_string(&response)
        .context("failed to serialize database tool response")?;
    tracing::debug!(
        event.name = "scribe.tool.database.completed",
        status = response.status,
        "database retrieval tool completed"
    );
    Ok(serialized)
}

/// Compatibility wrapper for callers that have not adopted structured hints.
pub async fn from_database(query: String) -> Result<String> {
    query_database(query, None, None, None).await
}

#[cfg(test)]
#[cfg_attr(coverage, coverage(off))]
mod tests {
    use std::sync::Mutex;

    use super::*;

    struct MockDbProvider {
        captured: Arc<Mutex<Option<DatabaseQuery>>>,
        reply: Option<String>,
    }

    struct ErrorProvider;

    #[async_trait::async_trait]
    impl DatabaseProvider for ErrorProvider {
        async fn query_database(
            &self,
            _query: &DatabaseQuery,
        ) -> Result<Option<String>> {
            Err(anyhow!("provider unavailable"))
        }
    }

    #[async_trait::async_trait]
    impl DatabaseProvider for MockDbProvider {
        async fn query_database(
            &self,
            query: &DatabaseQuery,
        ) -> Result<Option<String>> {
            let mut captured = self
                .captured
                .lock()
                .map_err(|error| anyhow!("capture lock poisoned: {error}"))?;
            *captured = Some(query.clone());
            Ok(self.reply.clone())
        }
    }

    #[test]
    fn query_normalizes_hints_and_bounds_result_limit() -> Result<()> {
        let query = DatabaseQuery::new(
            "  What changed?  ".to_string(),
            vec![" widget ".to_string(), "widget".to_string()],
            Some("  Q1  ".to_string()),
            Some(u16::MAX),
        )?;

        assert_eq!(query.question, "What changed?");
        assert_eq!(query.keywords, ["widget"]);
        assert_eq!(query.scope.as_deref(), Some("Q1"));
        assert_eq!(query.max_results, MAX_RESULT_LIMIT);
        let defaulted = DatabaseQuery::new(
            "Question".to_string(),
            vec![" ".to_string()],
            Some(" ".to_string()),
            Some(0),
        )?;
        assert!(defaulted.keywords.is_empty());
        assert!(defaulted.scope.is_none());
        assert_eq!(defaulted.max_results, 1);
        assert!(
            DatabaseQuery::new(" ".to_string(), Vec::new(), None, None)
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn tool_returns_structured_grounding_response() -> Result<()> {
        let captured = Arc::new(Mutex::new(None));
        let provider = Arc::new(MockDbProvider {
            captured: Arc::clone(&captured),
            reply: Some("verified evidence".to_string()),
        });

        let response = with_database_provider(
            provider,
            query_database(
                "What changed?".to_string(),
                Some(vec!["widget".to_string()]),
                Some("Q1".to_string()),
                Some(5),
            ),
        )
        .await?;
        let value: serde_json::Value = serde_json::from_str(&response)?;

        assert_eq!(value.get("status"), Some(&serde_json::json!("found")));
        assert_eq!(
            value.get("evidence"),
            Some(&serde_json::json!("verified evidence"))
        );
        let query = captured
            .lock()
            .map_err(|error| anyhow!("capture lock poisoned: {error}"))?
            .clone()
            .context("provider did not receive the query")?;
        assert_eq!(query.max_results, 5);
        Ok(())
    }

    #[tokio::test]
    async fn tool_returns_not_found_and_propagates_provider_errors()
    -> Result<()> {
        let response = with_database_provider(
            Arc::new(NoOpProvider),
            from_database("Unknown fact".to_string()),
        )
        .await?;
        let value: serde_json::Value = serde_json::from_str(&response)?;
        assert_eq!(value.get("status"), Some(&serde_json::json!("not_found")));
        assert_eq!(
            value.get("guidance"),
            Some(&serde_json::json!(NO_DATA_GUIDANCE))
        );

        assert!(
            with_database_provider(
                Arc::new(ErrorProvider),
                query_database("Question".to_string(), None, None, None),
            )
            .await
            .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn no_op_provider_returns_no_evidence() -> Result<()> {
        let query = DatabaseQuery::new(
            "Question".to_string(),
            Vec::new(),
            None,
            None,
        )?;

        assert!(NoOpProvider.query_database(&query).await?.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn request_scoped_providers_do_not_leak_between_tenants()
    -> Result<()> {
        let tenant_a = Arc::new(MockDbProvider {
            captured: Arc::new(Mutex::new(None)),
            reply: Some("tenant-a evidence".to_owned()),
        }) as Arc<dyn DatabaseProvider>;
        let tenant_b = Arc::new(MockDbProvider {
            captured: Arc::new(Mutex::new(None)),
            reply: Some("tenant-b evidence".to_owned()),
        }) as Arc<dyn DatabaseProvider>;

        let (answer_a, answer_b) = tokio::join!(
            with_database_provider(tenant_a, async {
                query_database("question".to_owned(), None, None, None).await
            }),
            with_database_provider(tenant_b, async {
                query_database("question".to_owned(), None, None, None).await
            }),
        );

        assert!(answer_a?.contains("tenant-a evidence"));
        assert!(answer_b?.contains("tenant-b evidence"));
        Ok(())
    }

    #[tokio::test]
    async fn database_tool_fails_closed_without_request_provider() {
        assert!(
            query_database("question".to_owned(), None, None, None)
                .await
                .is_err()
        );
    }
}
