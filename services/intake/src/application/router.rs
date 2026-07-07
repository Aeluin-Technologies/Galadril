//! Pipeline routing, multi-file discovery, lazy-loading, and conflict
//! resolution.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use moka::future::Cache;
use regex::Regex;

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
#[derive(Debug)]
struct PipelineRule {
    /// Identifier for debugging conflicts.
    source_id: String,
    /// Executable matcher.
    regex: Regex,
    /// Associated output state.
    route: ResolvedRoute,
}

/// Cached tenant state containing all compiled source limits.
#[derive(Debug)]
pub struct TenantRules {
    rules: Vec<PipelineRule>,
}

/// Directs incoming events to tenant-specific execution chains.
pub struct PipelineRouter {
    storage: Arc<dyn BlobStorage>,
    cache: Cache<String, Arc<TenantRules>>,
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
        let rules = self
            .cache
            .try_get_with(tenant.to_string(), self.fetch_tenant_rules(tenant))
            .await
            .map_err(|e| anyhow!("Failed to initialize routing state for tenant {tenant}: {e}"))?;

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

            let yaml_str = std::str::from_utf8(&data).with_context(|| {
                format!("Invalid UTF-8 in pipeline file {key}")
            })?;

            let config =
                parse_tenant_pipeline(yaml_str).with_context(|| {
                    format!("Malformed pipeline schema in {key}")
                })?;

            for source in config.sources {
                let regex =
                    Regex::new(&source.match_pattern).with_context(|| {
                        format!(
                            "Invalid regex '{}' in source '{}' within {key}",
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
