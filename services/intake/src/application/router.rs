//! Tenant-scoped published pipeline routing and bounded compiled-rule caching.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Result, anyhow, bail};
use moka::future::Cache;
use regex::{Regex, RegexBuilder};
use tokio::time::timeout;

use crate::application::pipeline::parse_tenant_pipeline;
use crate::domain::ports::{
    PipelineCatalog, PipelineIdentity, validate_pipeline_tenant,
};

/// Final destination attributes for a matched storage object.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedRoute {
    /// Immutable tenant pipeline publication selected by this route.
    pub identity: PipelineIdentity,
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
    /// Executable matcher.
    regex: Regex,
    /// Associated output state.
    route: ResolvedRoute,
    group_id: Option<String>,
    content_types: Vec<String>,
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
    storage: Arc<dyn PipelineCatalog>,
    cache: Cache<String, TenantCacheState>,
}

impl PipelineRouter {
    /// Mounts the concurrent cache engine.
    pub fn new(storage: Arc<dyn PipelineCatalog>, max_capacity: u64) -> Self {
        let cache = Cache::builder()
            .max_capacity(max_capacity)
            .time_to_live(Duration::from_secs(5))
            .build();

        Self { storage, cache }
    }

    /// Determines exact routing logic.
    ///
    /// Returns every matching publication and rejects ambiguity within one
    /// immutable pipeline identity.
    #[cfg(test)]
    pub async fn resolve_routes(
        &self,
        tenant: &str,
        s3_key: &str,
    ) -> Result<Vec<ResolvedRoute>> {
        self.resolve_event_routes(tenant, s3_key, "").await
    }

    /// Matches trusted storage coordinates independently of text modality.
    pub async fn resolve_event_routes(
        &self,
        tenant: &str,
        s3_key: &str,
        content_type: &str,
    ) -> Result<Vec<ResolvedRoute>> {
        validate_pipeline_tenant(tenant)?;
        self.storage.authorize_tenant(tenant)?;
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

        let mut matches: Vec<&ResolvedRoute> = Vec::new();
        let group = s3_key
            .strip_prefix(tenant)
            .and_then(|key| key.strip_prefix("/raw/"))
            .and_then(|key| key.split_once('/'))
            .map(|(group, _)| group);
        let media_type =
            content_type.split(';').next().unwrap_or_default().trim();

        for rule in &rules.rules {
            if rule
                .group_id
                .as_deref()
                .is_none_or(|expected| Some(expected) == group) &&
                (rule.content_types.is_empty() ||
                    rule.content_types.iter().any(|expected| {
                        expected.eq_ignore_ascii_case(media_type)
                    })) &&
                rule.regex.is_match(s3_key) &&
                !matches.contains(&&rule.route)
            {
                matches.push(&rule.route);
            }
        }

        match matches.len() {
            0 => bail!(
                "No pipeline source rule matches key {s3_key} for tenant {tenant}"
            ),
            1.. => {
                for (index, route) in matches.iter().enumerate() {
                    if matches.iter().skip(index + 1).any(|candidate| {
                        candidate.identity == route.identity &&
                            candidate != route
                    }) {
                        bail!(
                            "Ambiguous routing constraint within pipeline {}",
                            route.identity.execution_identity()
                        );
                    }
                }
                let mut resolved: Vec<ResolvedRoute> =
                    matches.into_iter().cloned().collect();
                resolved.sort_by(|left, right| {
                    (&left.identity.pipeline_id, &left.identity.revision_id)
                        .cmp(&(
                            &right.identity.pipeline_id,
                            &right.identity.revision_id,
                        ))
                });
                Ok(resolved)
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

    /// Compiles only database-published definitions for the requested tenant.
    async fn fetch_tenant_rules(
        &self,
        tenant: &str,
    ) -> Result<TenantCacheState> {
        let definitions = self.storage.published(tenant).await?;
        let mut compiled_rules = Vec::new();
        for published in definitions {
            if published.identity.tenant_id != tenant {
                bail!("Published pipeline tenant capability mismatch");
            }
            if published.definition.len() > 5 * 1024 * 1024 {
                bail!(
                    "Published pipeline exceeds maximum allowed size of 5MB"
                );
            }
            let config = parse_tenant_pipeline(&published.definition)?;
            for source in config.sources {
                if source.group_id.as_deref().is_some_and(|value| {
                    value.is_empty() ||
                        value.len() > 64 ||
                        !value.bytes().all(|byte| {
                            byte.is_ascii_alphanumeric() ||
                                byte == b'_' ||
                                byte == b'-'
                        })
                }) || source.content_types.len() > 16 ||
                    source.content_types.iter().any(|value| {
                        value.len() > 128 ||
                            !value.contains('/') ||
                            value.contains(';') ||
                            value.chars().any(char::is_whitespace)
                    })
                {
                    bail!("Invalid source ingestion filters");
                }
                let mut builder = RegexBuilder::new(&source.match_pattern);
                builder.size_limit(10 * 1024 * 1024);
                builder.dfa_size_limit(10 * 1024 * 1024);

                let regex = match builder.build() {
                    Ok(r) => r,
                    Err(e) => {
                        return Ok(TenantCacheState::InvalidConfig(format!(
                            "Invalid or overly complex regex '{}' in source '{}' for tenant {tenant}: {e}",
                            source.match_pattern, source.id
                        )));
                    },
                };

                compiled_rules.push(PipelineRule {
                    regex,
                    group_id: source.group_id,
                    content_types: source.content_types,
                    route: ResolvedRoute {
                        identity: published.identity.clone(),
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
    use anyhow::Context as _;

    use super::*;
    use crate::domain::ports::{
        PipelineCatalog, PipelineIdentity, PublishedPipeline,
    };

    struct Catalog;

    #[async_trait::async_trait]
    impl PipelineCatalog for Catalog {
        fn authorize_tenant(&self, tenant: &str) -> Result<()> {
            if tenant == "tenant_a" {
                Ok(())
            } else {
                bail!("tenant capability unavailable")
            }
        }

        async fn published(
            &self,
            tenant: &str,
        ) -> Result<Vec<PublishedPipeline>> {
            if tenant == "tenant_a" {
                Ok(vec![PublishedPipeline {
                    identity: PipelineIdentity::new(
                        tenant, "daily", "revision_a",
                    )?,
                    definition: r#"{"sources":[{"id":"images","topic":"raw","match_pattern":"^images/","parser":"image","source_kind":"camera"}]}"#.to_owned(),
                }])
            } else {
                Ok(Vec::new())
            }
        }
    }

    #[tokio::test]
    async fn published_routes_are_isolated_and_invalid_tenants_fail()
    -> Result<()> {
        let router = PipelineRouter::new(Arc::new(Catalog), 10);
        assert_eq!(
            router
                .resolve_routes("tenant_a", "images/a.jpg")
                .await?
                .first()
                .context("route is missing")?
                .source_id,
            "images"
        );
        assert!(
            router
                .resolve_routes("tenant_b", "images/a.jpg")
                .await
                .is_err()
        );
        assert!(
            router
                .resolve_routes("tenant_a/../tenant_b", "images/a.jpg")
                .await
                .is_err()
        );
        Ok(())
    }

    struct SharedSourceCatalog;

    #[async_trait::async_trait]
    impl PipelineCatalog for SharedSourceCatalog {
        fn authorize_tenant(&self, _: &str) -> Result<()> {
            Ok(())
        }

        async fn published(
            &self,
            tenant: &str,
        ) -> Result<Vec<PublishedPipeline>> {
            let definition = r#"{"sources":[{"id":"images","topic":"raw","match_pattern":"^images/","parser":"image","source_kind":"camera"}]}"#;
            Ok(vec![
                PublishedPipeline {
                    identity: PipelineIdentity::new(
                        tenant,
                        "daily",
                        "revision_a",
                    )?,
                    definition: definition.to_owned(),
                },
                PublishedPipeline {
                    identity: PipelineIdentity::new(
                        tenant,
                        "archive",
                        "revision_b",
                    )?,
                    definition: definition.to_owned(),
                },
            ])
        }
    }

    #[tokio::test]
    async fn shared_sources_preserve_each_immutable_pipeline() -> Result<()> {
        let router = PipelineRouter::new(Arc::new(SharedSourceCatalog), 10);

        let routes = router.resolve_routes("tenant_a", "images/a.jpg").await?;
        assert_eq!(routes.len(), 2);
        assert_eq!(
            routes
                .first()
                .context("archive route is missing")?
                .identity
                .pipeline_id,
            "archive"
        );
        assert_eq!(
            routes
                .get(1)
                .context("daily route is missing")?
                .identity
                .pipeline_id,
            "daily"
        );
        assert!(
            routes
                .iter()
                .all(|route| route.identity.tenant_id == "tenant_a")
        );
        Ok(())
    }

    struct BrokenCatalog;

    struct MailCatalog;

    #[async_trait::async_trait]
    impl PipelineCatalog for MailCatalog {
        fn authorize_tenant(&self, _: &str) -> Result<()> {
            Ok(())
        }

        async fn published(
            &self,
            tenant: &str,
        ) -> Result<Vec<PublishedPipeline>> {
            Ok(vec![PublishedPipeline {
                identity: PipelineIdentity::new(tenant, "mail", "revision")?,
                definition: r#"{"sources":[{"id":"mail","topic":"raw","match_pattern":".*","parser":"text","group_id":"email","content_types":["text/plain"],"source_kind":"email"}]}"#.to_owned(),
            }])
        }
    }

    #[tokio::test]
    async fn semantic_source_filters_do_not_confuse_all_text_with_email()
    -> Result<()> {
        let router = PipelineRouter::new(Arc::new(MailCatalog), 10);
        let routes = router
            .resolve_event_routes(
                "tenant_a",
                "tenant_a/raw/email/message.txt",
                "text/plain; charset=utf-8",
            )
            .await?;
        assert_eq!(
            routes.first().context("mail route missing")?.source_kind,
            "email"
        );
        for (key, media) in [
            ("tenant_a/raw/default/document.txt", "text/plain"),
            ("tenant_a/raw/email/photo.jpg", "image/jpeg"),
            ("tenant_b/raw/email/message.txt", "text/plain"),
        ] {
            assert!(
                router
                    .resolve_event_routes("tenant_a", key, media)
                    .await
                    .is_err()
            );
        }
        Ok(())
    }

    #[async_trait::async_trait]
    impl PipelineCatalog for BrokenCatalog {
        fn authorize_tenant(&self, _: &str) -> Result<()> {
            Ok(())
        }

        async fn published(&self, _: &str) -> Result<Vec<PublishedPipeline>> {
            bail!("database unavailable")
        }
    }

    #[tokio::test]
    async fn unavailable_database_fails_closed() {
        let router = PipelineRouter::new(Arc::new(BrokenCatalog), 10);
        assert!(
            router
                .resolve_routes("tenant_a", "images/a.jpg")
                .await
                .is_err()
        );
    }
}
