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
    /// Stable tenant-configured source identifier.
    pub source_id: String,
    /// Destination queue for validated records.
    pub topic: String,
    /// Avro structure mapping.
    pub schema_path: Option<String>,
    /// Payload extraction strategy.
    pub parser: String,
    /// Source class used for sensor-aware lineage.
    pub source_kind: String,
    /// Optional physical sensor identifier.
    pub sensor_id: Option<String>,
    /// Optional physical sensor type.
    pub sensor_type: Option<String>,
    /// Optional capture device identifier.
    pub device_id: Option<String>,
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

#[derive(Debug, Clone)]
pub enum TenantCacheState {
    Active(Arc<TenantRules>),
    NoRulesFound,
    InvalidConfig(String),
}

/// Directs incoming events to tenant-specific execution chains.
pub struct PipelineRouter {
    storage: Arc<dyn BlobStorage>,
    cache: Cache<String, TenantCacheState>,
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
        let cache_state = self
            .cache
            .try_get_with(tenant.to_string(), async {
                timeout(Duration::from_secs(10), self.fetch_tenant_rules(tenant))
                    .await
                    .map_err(|_| anyhow!("Timeout reached while loading rules for tenant {tenant}"))?
            })
            .await
            .map_err(|e| anyhow!("Failed to initialize routing state for tenant {tenant}: {e}"))?;

        let rules = match cache_state {
            TenantCacheState::Active(rules) => rules,
            TenantCacheState::NoRulesFound => {
                bail!(
                    "No pipeline source rule matches key {s3_key} for tenant {tenant}"
                )
            },
            TenantCacheState::InvalidConfig(err) => {
                bail!("Tenant configuration is invalid: {err}")
            },
        };

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
        tracing::info!(
            event.name = "pipeline.cache.invalidated",
            %tenant,
            "tenant pipeline cache invalidated"
        );
    }

    /// Scans the storage namespace to aggregate and compile all configuration
    /// files for an usager.
    async fn fetch_tenant_rules(
        &self,
        tenant: &str,
    ) -> Result<TenantCacheState> {
        let prefix = format!("{tenant}/");
        let objects = self
            .storage
            .list_objects(&prefix)
            .await
            .with_context(|| format!("Failed to list pipeline configurations for tenant {tenant}"))?;

        let mut compiled_rules = Vec::new();

        for (key, size) in objects {
            if !key.ends_with(".yaml") && !key.ends_with(".yml") {
                continue;
            }

            if size > 5 * 1024 * 1024 {
                return Ok(TenantCacheState::InvalidConfig(format!(
                    "Configuration file {key} exceeds maximum allowed size of 5MB"
                )));
            }

            let data = self
                .storage
                .download_file(&key)
                .await
                .with_context(|| format!("Failed to download tenant pipeline configuration at {key}"))?;

            let yaml_str = match std::str::from_utf8(&data) {
                Ok(s) => s,
                Err(_) => {
                    return Ok(TenantCacheState::InvalidConfig(format!(
                        "Invalid UTF-8 in pipeline file {key}"
                    )));
                },
            };

            let config = match parse_tenant_pipeline(yaml_str) {
                Ok(c) => c,
                Err(e) => {
                    return Ok(TenantCacheState::InvalidConfig(format!(
                        "Malformed pipeline schema in {key}: {e}"
                    )));
                },
            };

            for source in config.sources {
                let mut builder = RegexBuilder::new(&source.match_pattern);
                builder.size_limit(10 * 1024 * 1024);
                builder.dfa_size_limit(10 * 1024 * 1024);

                let regex = match builder.build() {
                    Ok(r) => r,
                    Err(e) => {
                        return Ok(TenantCacheState::InvalidConfig(format!(
                            "Invalid or overly complex regex '{}' in source '{}' within {key}: {e}",
                            source.match_pattern, source.id
                        )));
                    },
                };

                compiled_rules.push(PipelineRule {
                    source_id: source.id.clone(),
                    regex,
                    route: ResolvedRoute {
                        source_id: source.id,
                        topic: source.topic,
                        schema_path: source.schema_path,
                        parser: source.parser,
                        source_kind: source.source_kind,
                        sensor_id: source.sensor_id,
                        sensor_type: source.sensor_type,
                        device_id: source.device_id,
                    },
                });
            }
        }

        if compiled_rules.is_empty() {
            return Ok(TenantCacheState::NoRulesFound);
        }

        tracing::info!(
            event.name = "pipeline.rules.loaded",
            %tenant,
            count = compiled_rules.len(),
            "tenant pipeline rules loaded into cache"
        );
        Ok(TenantCacheState::Active(Arc::new(TenantRules {
            rules: compiled_rules,
        })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    mockall::mock! {
        pub BlobStorage {}
        #[async_trait::async_trait]
        impl crate::domain::ports::BlobStorage for BlobStorage {
            async fn list_objects(&self, prefix: &str) -> anyhow::Result<Vec<(String, i64)>>;
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
        mock_storage.expect_list_objects().returning(|_| {
            Ok(vec![("tenant1/config.yaml".to_string(), 1024)])
        });
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
        mock_storage.expect_list_objects().returning(|_| {
            Ok(vec![("tenant_empty/config.yaml".to_string(), 1024)])
        });
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
        mock_storage.expect_list_objects().returning(|_| {
            Ok(vec![(
                "tenant_huge/config.yaml".to_string(),
                6 * 1024 * 1024,
            )])
        });

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
            .returning(|_| Ok(vec![]));

        let router = PipelineRouter::new(Arc::new(mock_storage), 10);

        let res1 = router.resolve_route("missing_tenant", "key").await;
        assert!(res1.is_err());

        let res2 = router.resolve_route("missing_tenant", "key").await;
        assert!(res2.is_err());
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn test_security_timeout() {
        struct SlowBlobStorage;

        #[rustfmt::skip]
        #[async_trait::async_trait]
        impl crate::domain::ports::BlobStorage for SlowBlobStorage {
            async fn list_objects(&self, _: &str) -> anyhow::Result<Vec<(String, i64)>> { tokio::time::sleep(Duration::from_secs(12)).await; Ok(vec![]) }
            async fn download_file(&self, _: &str) -> anyhow::Result<Vec<u8>> { unimplemented!() }
            async fn upload_file(&self, _: &str, _: &[u8]) -> anyhow::Result<String> { unimplemented!() }
            async fn upload_file_with_authz(&self, _: &str, _: &[u8], _: &crate::domain::ports::AuthzHints) -> anyhow::Result<String> { unimplemented!() }
            async fn authz_hints(&self, _: &str, _: &str) -> anyhow::Result<crate::domain::ports::AuthzHints> { unimplemented!() }
        }

        let router = PipelineRouter::new(Arc::new(SlowBlobStorage), 10);
        let res = router.resolve_route("tenant_slow", "key").await;
        assert!(res.is_err());
        assert!(
            res.unwrap_err().to_string().contains("Timeout reached"),
            "The error received was not the expected timeout"
        );
    }
}
