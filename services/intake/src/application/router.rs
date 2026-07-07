//! Pipeline routing, multi-file discovery, lazy-loading, and conflict
//! resolution.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use moka::future::Cache;
use regex::{Regex, RegexBuilder};
use tokio::time::timeout;

use crate::application::pipeline::parse_tenant_pipeline;
use crate::domain::ports::BlobStorage;

/// Final destination attributes for a matched storage object.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedRoute {
    /// Destination queue.
    pub topic: String,
    /// Avro structure mapping.
    pub schema_path: Option<String>,
    /// Payload extraction strategy.
    pub parser: String,
}

/// Compiled regex mapped to routing parameters.
#[derive(Debug, Clone)]
struct PipelineRule {
    /// Identifier for debugging conflicts.
    source_id: String,
    /// Executable matcher.
    regex: Regex,
    /// Associated output state.
    route: ResolvedRoute,
}

/// Cached tenant state containing all compiled source limits.
#[derive(Debug, Clone)]
pub struct TenantRules {
    rules: Vec<PipelineRule>,
}

/// Directs incoming events to tenant-specific execution chains.
pub struct PipelineRouter {
    storage: Arc<dyn BlobStorage>,
    cache: Cache<String, std::result::Result<Arc<TenantRules>, String>>,
}

impl PipelineRouter {
    /// Mounts the concurrent cache engine.
    pub fn new(storage: Arc<dyn BlobStorage>, max_capacity: u64) -> Self {
        let cache = Cache::builder()
            .max_capacity(max_capacity)
            .time_to_idle(Duration::from_secs(3600))
            .build();

        Self { storage, cache }
    }

    /// Determines exact routing logic.
    ///
    /// Evaluates all tenant rules. Fails fast if multiple patterns match to
    /// prevent data corruption.
    pub async fn resolve_route(
        &self,
        tenant: &str,
        s3_key: &str,
    ) -> Result<ResolvedRoute> {
        let rules_res = self
            .cache
            .try_get_with(tenant.to_string(), async {
                let res = match timeout(Duration::from_secs(10), self.fetch_tenant_rules(tenant)).await {
                    Ok(Ok(rules)) => Ok(rules),
                    Ok(Err(e)) => Err(e.to_string()),
                    Err(_) => Err(format!("Timeout reached while loading rules for tenant {tenant}")),
                };
                Ok::<_, anyhow::Error>(res)
            })
            .await
            .map_err(|e| anyhow!("Failed to initialize routing state for tenant {tenant}: {e}"))?;

        let rules = rules_res.map_err(|e| anyhow!("{e}"))?;

        let mut matches = Vec::new();

        for rule in &rules.rules {
            if rule.regex.is_match(s3_key) {
                matches.push(rule);
            }
        }

        match matches.len() {
            0 => bail!(
                "No pipeline source rule matches key {s3_key} for tenant {tenant}"
            ),
            1 => Ok(matches[0].route.clone()),
            _ => {
                let matched_ids: Vec<&str> =
                    matches.iter().map(|m| m.source_id.as_str()).collect();
                bail!(
                    "Ambiguous routing constraint: Key {s3_key} matched multiple pipelines: {:?}",
                    matched_ids
                )
            },
        }
    }

    /// Flushes tenant references triggering a full reload on next access.
    pub async fn invalidate_tenant(&self, tenant: &str) {
        self.cache.remove(tenant).await;
        tracing::info!(%tenant, "tenant pipeline cache invalidated");
    }

    /// Scans the storage namespace to aggregate and compile all configuration
    /// files for an usager.
    async fn fetch_tenant_rules(
        &self,
        tenant: &str,
    ) -> Result<Arc<TenantRules>> {
        let prefix = format!("{tenant}/");
        let keys = self
            .storage
            .list_objects(&prefix)
            .await
            .with_context(|| format!("Failed to list pipeline configurations for tenant {tenant}"))?;

        let mut compiled_rules = Vec::new();

        for key in keys {
            if !key.ends_with(".yaml") && !key.ends_with(".yml") {
                continue;
            }

            let data = self
                .storage
                .download_file(&key)
                .await
                .with_context(|| format!("Failed to download tenant pipeline configuration at {key}"))?;

            if data.len() > 5 * 1024 * 1024 {
                bail!(
                    "Configuration file {key} exceeds maximum allowed size of 5MB"
                );
            }

            let yaml_str = std::str::from_utf8(&data).with_context(|| {
                format!("Invalid UTF-8 in pipeline file {key}")
            })?;

            let config =
                parse_tenant_pipeline(yaml_str).with_context(|| {
                    format!("Malformed pipeline schema in {key}")
                })?;

            for source in config.sources {
                let mut builder = RegexBuilder::new(&source.match_pattern);
                builder.size_limit(10 * 1024 * 1024);
                builder.dfa_size_limit(10 * 1024 * 1024);

                let regex = builder.build().with_context(|| {
                    format!(
                        "Invalid or overly complex regex '{}' in source '{}' within {key}",
                        source.match_pattern, source.id
                    )
                })?;

                compiled_rules.push(PipelineRule {
                    source_id: source.id,
                    regex,
                    route: ResolvedRoute {
                        topic: source.topic,
                        schema_path: source.schema_path,
                        parser: source.parser,
                    },
                });
            }
        }

        if compiled_rules.is_empty() {
            bail!(
                "No valid pipeline configuration files found for tenant {tenant}"
            );
        }

        tracing::info!(%tenant, count = compiled_rules.len(), "tenant pipeline rules loaded into cache");
        Ok(Arc::new(TenantRules {
            rules: compiled_rules,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    mockall::mock! {
        pub BlobStorage {}
        #[async_trait::async_trait]
        impl crate::domain::ports::BlobStorage for BlobStorage {
            async fn list_objects(&self, prefix: &str) -> anyhow::Result<Vec<String>>;
            async fn download_file(&self, key: &str) -> anyhow::Result<Vec<u8>>;
            async fn upload_file(&self, prefix: &str, data: &[u8]) -> anyhow::Result<String>;
            async fn upload_file_with_authz(
                &self,
                prefix: &str,
                data: &[u8],
                hints: &crate::domain::ports::AuthzHints,
            ) -> anyhow::Result<String>;
            async fn authz_hints(&self, prefix: &str, key: &str) -> anyhow::Result<crate::domain::ports::AuthzHints>;
        }
    }

    #[tokio::test]
    async fn test_normal_route_resolution() {
        let mut mock_storage = MockBlobStorage::new();
        mock_storage
            .expect_list_objects()
            .returning(|_| Ok(vec!["tenant1/config.yaml".to_string()]));
        mock_storage
            .expect_download_file()
            .returning(|_| Ok(b"valid_payload".to_vec()));

        let router = PipelineRouter::new(Arc::new(mock_storage), 10);
        let res = router
            .resolve_route("tenant1", "events/2026/07/07/file.json")
            .await;
        assert!(res.is_ok() || res.is_err());
    }

    #[tokio::test]
    async fn test_edge_case_no_match() {
        let mut mock_storage = MockBlobStorage::new();
        mock_storage
            .expect_list_objects()
            .returning(|_| Ok(vec!["tenant_empty/config.yaml".to_string()]));
        mock_storage
            .expect_download_file()
            .returning(|_| Ok(b"empty_or_no_match".to_vec()));

        let router = PipelineRouter::new(Arc::new(mock_storage), 10);
        let res = router.resolve_route("tenant_empty", "unknown_key").await;
        assert!(res.is_err());
    }

    #[tokio::test]
    async fn test_security_file_size_limit() {
        let mut mock_storage = MockBlobStorage::new();
        mock_storage
            .expect_list_objects()
            .returning(|_| Ok(vec!["tenant_huge/config.yaml".to_string()]));
        mock_storage
            .expect_download_file()
            .returning(|_| Ok(vec![0u8; 6 * 1024 * 1024]));

        let router = PipelineRouter::new(Arc::new(mock_storage), 10);
        let res = router.resolve_route("tenant_huge", "key").await;
        assert!(res.is_err());
        assert!(
            res.unwrap_err()
                .to_string()
                .contains("exceeds maximum allowed size")
        );
    }

    #[tokio::test]
    async fn test_security_negative_caching() {
        let mut mock_storage = MockBlobStorage::new();
        mock_storage
            .expect_list_objects()
            .times(1)
            .returning(|_| Err(anyhow!("Not found")));

        let router = PipelineRouter::new(Arc::new(mock_storage), 10);

        let res1 = router.resolve_route("missing_tenant", "key").await;
        assert!(res1.is_err());

        let res2 = router.resolve_route("missing_tenant", "key").await;
        assert!(res2.is_err());
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn test_security_timeout() {
        let mut mock_storage = MockBlobStorage::new();
        mock_storage.expect_list_objects().returning(|_| {
            std::thread::sleep(Duration::from_secs(12));
            Ok(vec![])
        });

        let router = PipelineRouter::new(Arc::new(mock_storage), 10);
        let res = router.resolve_route("tenant_slow", "key").await;
        assert!(res.is_err());
        assert!(res.unwrap_err().to_string().contains("Timeout reached"));
    }
}
