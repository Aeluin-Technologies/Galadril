//! Root-scoped causal summaries are independent of raw input visibility.

use std::sync::Arc;

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};

use crate::application::ports::causal_store::CausalStore;
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RootEstimate {
    effect_size: f64,
    confidence_score: f64,
    time_lag_seconds: f64,
    p_value: Option<f64>,
    q_value: Option<f64>,
    stability: f64,
    supports_counterfactual: bool,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default)]
pub struct CausalSummary {
    causal_links: usize,
    validated_effects: usize,
    observation_count: usize,
    counterfactual_ready: bool,
    root_estimates: Vec<RootEstimate>,
}

#[derive(Serialize)]
pub struct AuthorizedAnalysis {
    pub cache_key: String,
    pub summary: CausalSummary,
}

pub struct CausalService {
    store: Arc<dyn CausalStore>,
    auth: Arc<dyn Authorization>,
}

impl CausalService {
    pub fn new(
        store: Arc<dyn CausalStore>,
        auth: Arc<dyn Authorization>,
    ) -> Self {
        Self { store, auth }
    }

    /// Root access authorizes its statistical result, never its private
    /// inputs.
    pub async fn latest(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        entity_id: &str,
    ) -> Result<Option<AuthorizedAnalysis>> {
        let context = QueryContext {
            entity_id: Some(entity_id.to_owned()),
            modality: None,
            state_type: None,
            ..context.clone()
        };
        if !self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                Permission::View,
                "entity_state",
                entity_id,
                Some(&context),
            )
            .await?
        {
            return Ok(None);
        }
        let Some(run) =
            self.store.latest_for_entity(tenant_id, entity_id).await?
        else {
            return Ok(None);
        };
        let summary: CausalSummary = serde_json::from_value(run.summary)?;
        ensure!(
            summary.root_estimates.len() <= 256,
            "Analysis exceeds limit"
        );
        Ok(Some(AuthorizedAnalysis {
            cache_key: run.cache_key,
            summary,
        }))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use anyhow::Result;

    use super::*;
    use crate::application::ports::causal_store::CausalRun;

    struct Runs;

    #[async_trait::async_trait]
    impl CausalStore for Runs {
        async fn latest_for_entity(
            &self,
            tenant: &str,
            _: &str,
        ) -> Result<Option<CausalRun>> {
            if tenant != "tenant_a" {
                return Ok(None);
            }
            Ok(Some(CausalRun {
                cache_key: "run_a".into(),
                summary: serde_json::json!({"causal_links": 2, "evidence_sources": [{"resource_id": "evt_private"}], "chain": ["private_neighbor"]}),
            }))
        }
    }

    struct Access;

    #[async_trait::async_trait]
    impl Authorization for Access {
        async fn upsert_relationship(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn delete_relationship(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<()> {
            Ok(())
        }

        async fn invalidate_tenant_cache(&self, _: &str) {}

        async fn is_authorized(
            &self,
            user: &str,
            _: &str,
            _: Permission,
            kind: &str,
            _: &str,
            context: Option<&QueryContext>,
        ) -> Result<bool> {
            Ok(user == "owner" &&
                kind == "entity_state" &&
                context.is_some_and(|context| {
                    context.entity_id.as_deref() == Some("entity_a")
                }))
        }
    }

    #[tokio::test]
    async fn root_analysis_is_visible_without_revealing_private_inputs_or_chain()
    -> Result<()> {
        let service = CausalService::new(Arc::new(Runs), Arc::new(Access));
        let context = QueryContext::default();
        assert!(
            service
                .latest("tenant_a", "reader", &context, "entity_a")
                .await?
                .is_none()
        );
        let run = service
            .latest("tenant_a", "owner", &context, "entity_a")
            .await?;
        let serialized = serde_json::to_value(run)?;
        assert_eq!(
            serialized
                .get("summary")
                .and_then(|summary| summary.get("causal_links")),
            Some(&serde_json::json!(2))
        );
        assert!(!serialized.to_string().contains("evt_private"));
        assert!(!serialized.to_string().contains("private_neighbor"));
        assert!(
            service
                .latest("tenant_b", "owner", &context, "entity_a")
                .await?
                .is_none()
        );
        Ok(())
    }
}
