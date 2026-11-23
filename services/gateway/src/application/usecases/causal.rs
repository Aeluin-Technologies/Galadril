//! Releases causal summaries under their signed, revocable GOL provenance.

use std::sync::Arc;

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::application::ports::causal_store::CausalStore;
use crate::application::ports::conversation_store::EvidenceSource;
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

#[derive(Deserialize)]
struct CausalColumns {
    causal_links: String,
    validated_effects: String,
    observation_count: String,
    counterfactual_ready: bool,
    effect_sizes: Vec<f64>,
    confidence_scores: Vec<f64>,
    lag_seconds: Vec<f64>,
    p_values: Vec<Option<f64>>,
    q_values: Vec<Option<f64>>,
    stabilities: Vec<f64>,
    counterfactual_flags: Vec<bool>,
}

impl CausalColumns {
    fn into_summary(self) -> Result<CausalSummary> {
        let length = self.effect_sizes.len();
        ensure!(
            length <= 256 &&
                [
                    self.confidence_scores.len(),
                    self.lag_seconds.len(),
                    self.p_values.len(),
                    self.q_values.len(),
                    self.stabilities.len(),
                    self.counterfactual_flags.len(),
                ]
                .iter()
                .all(|size| *size == length),
            "Invalid causal estimate columns"
        );
        let root_estimates = self
            .effect_sizes
            .into_iter()
            .zip(self.confidence_scores)
            .zip(self.lag_seconds)
            .zip(self.p_values)
            .zip(self.q_values)
            .zip(self.stabilities)
            .zip(self.counterfactual_flags)
            .map(
                |(
                    (
                        (
                            (
                                (
                                    (effect_size, confidence_score),
                                    time_lag_seconds,
                                ),
                                p_value,
                            ),
                            q_value,
                        ),
                        stability,
                    ),
                    supports_counterfactual,
                )| RootEstimate {
                    effect_size,
                    confidence_score,
                    time_lag_seconds,
                    p_value,
                    q_value,
                    stability,
                    supports_counterfactual,
                },
            )
            .collect();
        Ok(CausalSummary {
            causal_links: self.causal_links.parse()?,
            validated_effects: self.validated_effects.parse()?,
            observation_count: self.observation_count.parse()?,
            counterfactual_ready: self.counterfactual_ready,
            root_estimates,
        })
    }
}

pub(super) const RELEASE_FIELDS: &[&str] = &[
    "cache_key",
    "target",
    "status",
    "causal_links",
    "validated_effects",
    "observation_count",
    "counterfactual_ready",
    "effect_sizes",
    "confidence_scores",
    "lag_seconds",
    "p_values",
    "q_values",
    "stabilities",
    "counterfactual_flags",
];

#[derive(Deserialize)]
pub(super) struct CausalEnvelope {
    pub gol_id: String,
    pub gol_receipt: String,
    pub gol_symbol_key: String,
    pub gol_value: Value,
}

pub(super) const EVIDENCE_TYPE: &str = "causal_run";

#[async_trait::async_trait]
pub(super) trait CausalRelease: Send + Sync {
    async fn authorize(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        envelope: &CausalEnvelope,
    ) -> Result<()>;
}

#[async_trait::async_trait]
impl CausalRelease for super::gol::GolService {
    async fn authorize(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        envelope: &CausalEnvelope,
    ) -> Result<()> {
        self.verify_record_release(
            (tenant, user),
            context,
            (
                &envelope.gol_id,
                &envelope.gol_symbol_key,
                &envelope.gol_receipt,
            ),
            &envelope.gol_value,
            RELEASE_FIELDS,
        )
        .await
    }
}

#[derive(Serialize)]
pub struct AuthorizedAnalysis {
    pub cache_key: String,
    pub summary: CausalSummary,
    #[serde(skip)]
    receipt_hash: String,
}

impl AuthorizedAnalysis {
    pub(super) fn evidence_id(&self) -> String {
        format!("{}:{}", self.cache_key, self.receipt_hash)
    }
}

pub struct CausalService {
    store: Arc<dyn CausalStore>,
    auth: Arc<dyn Authorization>,
    release: Arc<dyn CausalRelease>,
}

impl CausalService {
    fn with_release(
        store: Arc<dyn CausalStore>,
        auth: Arc<dyn Authorization>,
        release: Arc<dyn CausalRelease>,
    ) -> Self {
        Self {
            store,
            auth,
            release,
        }
    }

    pub(crate) fn new(
        store: Arc<dyn CausalStore>,
        auth: Arc<dyn Authorization>,
        gol: Arc<super::gol::GolService>,
    ) -> Self {
        Self::with_release(store, auth, gol)
    }

    pub async fn latest(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        entity_id: &str,
    ) -> Result<Option<AuthorizedAnalysis>> {
        self.read(tenant_id, user_id, context, entity_id, None)
            .await
    }

    /// An answer can be replayed while its exact computation remains visible.
    pub(super) async fn replayable(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        entity_id: &str,
        evidence_id: &str,
    ) -> Result<bool> {
        let Some((cache_key, receipt_hash)) = evidence_id.split_once(':')
        else {
            return Ok(false);
        };
        Ok(self
            .read(
                tenant_id,
                user_id,
                context,
                entity_id,
                Some((cache_key, receipt_hash)),
            )
            .await?
            .is_some())
    }

    async fn read(
        &self,
        tenant_id: &str,
        user_id: &str,
        context: &QueryContext,
        entity_id: &str,
        expected: Option<(&str, &str)>,
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
        let Some(run) = self
            .store
            .for_entity(tenant_id, entity_id, expected.map(|(key, _)| key))
            .await?
        else {
            return Ok(None);
        };
        let Ok(envelope) =
            serde_json::from_value::<CausalEnvelope>(run.summary)
        else {
            tracing::warn!(
                event.name = "gol.causal.release_denied",
                reason = "missing_provenance"
            );
            return Ok(None);
        };
        let receipt_hash =
            format!("{:x}", Sha256::digest(envelope.gol_receipt.as_bytes()));
        let value = &envelope.gol_value;
        if value.get("cache_key").and_then(Value::as_str) !=
            Some(run.cache_key.as_str()) ||
            value.get("target").and_then(Value::as_str) !=
                Some(format!("entity:{entity_id}").as_str()) ||
            value.get("status").and_then(Value::as_str) != Some("success") ||
            expected.is_some_and(|(key, hash)| {
                key != run.cache_key || hash != receipt_hash
            })
        {
            tracing::warn!(
                event.name = "gol.causal.release_denied",
                reason = "scope_mismatch"
            );
            return Ok(None);
        }
        if self
            .release
            .authorize(tenant_id, user_id, &context, &envelope)
            .await
            .is_err()
        {
            tracing::warn!(
                event.name = "gol.causal.release_denied",
                reason = "authorization"
            );
            return Ok(None);
        }
        let summary =
            CausalColumns::deserialize(envelope.gol_value)?.into_summary()?;
        Ok(Some(AuthorizedAnalysis {
            cache_key: run.cache_key,
            receipt_hash,
            summary,
        }))
    }
}

pub(super) async fn causal_evidence_visible(
    service: Option<&CausalService>,
    (tenant, user): (&str, &str),
    context: &QueryContext,
    source: &EvidenceSource,
) -> Result<bool> {
    let (Some(service), Some(entity)) = (service, source.entity_id.as_deref())
    else {
        return Ok(false);
    };
    service
        .replayable(tenant, user, context, entity, &source.resource_id)
        .await
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use anyhow::Result;

    use super::*;
    use crate::application::ports::causal_store::CausalRun;

    struct Runs;

    struct Release;

    #[async_trait::async_trait]
    impl CausalRelease for Release {
        async fn authorize(
            &self,
            _: &str,
            user: &str,
            _: &QueryContext,
            _: &CausalEnvelope,
        ) -> Result<()> {
            ensure!(user == "owner", "Required input revoked");
            Ok(())
        }
    }

    #[async_trait::async_trait]
    impl CausalStore for Runs {
        async fn for_entity(
            &self,
            tenant: &str,
            _: &str,
            _: Option<&str>,
        ) -> Result<Option<CausalRun>> {
            if tenant != "tenant_a" {
                return Ok(None);
            }
            Ok(Some(CausalRun {
                cache_key: "run_a".into(),
                summary: serde_json::from_slice(
                    br#"{
                        "gol_receipt": "evt_private",
                        "gol_symbol_key": "test::CausalRun",
                        "gol_id": "private_neighbor",
                        "gol_value": {
                            "cache_key": "run_a",
                            "target": "entity:entity_a",
                            "status": "success",
                            "causal_links": "2",
                            "validated_effects": "2",
                            "observation_count": "30",
                            "counterfactual_ready": true,
                            "effect_sizes": [51.248178375505404, -36.573994842753436],
                            "confidence_scores": [0.9311137037688033, 0.5],
                            "lag_seconds": [52.314008204106244, 1.0],
                            "p_values": [2.0030397744267762e-253, null],
                            "q_values": [null, 0.05],
                            "stabilities": [0.9745365320034685, 0.75],
                            "counterfactual_flags": [true, false]
                        }
                    }"#,
                )?,
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
            Ok(matches!(user, "owner" | "root_reader") &&
                kind == "entity_state" &&
                context.is_some_and(|context| {
                    context.entity_id.as_deref() == Some("entity_a")
                }))
        }
    }

    #[tokio::test]
    async fn root_estimates_preserve_persisted_floating_point_statistics()
    -> Result<()> {
        let service = CausalService::with_release(
            Arc::new(Runs),
            Arc::new(Access),
            Arc::new(Release),
        );
        let run = service
            .latest("tenant_a", "owner", &QueryContext::default(), "entity_a")
            .await?;
        let encoded = serde_json::to_vec(&run)?;
        let replayed: serde_json::Value = serde_json::from_slice(&encoded)?;
        let estimates = replayed
            .get("summary")
            .and_then(|summary| summary.get("root_estimates"));
        assert_eq!(
            estimates,
            Some(&serde_json::json!([{
                "effect_size": 51.248178375505404_f64,
                "confidence_score": 0.9311137037688033_f64,
                "time_lag_seconds": 52.314008204106244_f64,
                "p_value": 2.0030397744267762e-253_f64,
                "q_value": null,
                "stability": 0.9745365320034685_f64,
                "supports_counterfactual": true
            }, {
                "effect_size": -36.573994842753436_f64,
                "confidence_score": 0.5_f64,
                "time_lag_seconds": 1.0_f64,
                "p_value": null,
                "q_value": 0.05_f64,
                "stability": 0.75_f64,
                "supports_counterfactual": false
            }]))
        );
        Ok(())
    }

    #[tokio::test]
    async fn root_grants_do_not_release_analysis_without_its_inputs()
    -> Result<()> {
        let service = CausalService::with_release(
            Arc::new(Runs),
            Arc::new(Access),
            Arc::new(Release),
        );
        let context = QueryContext::default();
        assert!(
            service
                .latest("tenant_a", "root_reader", &context, "entity_a")
                .await?
                .is_none()
        );
        let run = service
            .latest("tenant_a", "owner", &context, "entity_a")
            .await?
            .ok_or_else(|| anyhow::anyhow!("Missing signed analysis"))?;
        let serialized = serde_json::to_value(&run)?;
        assert_eq!(
            serialized
                .get("summary")
                .and_then(|summary| summary.get("causal_links")),
            Some(&serde_json::json!(2))
        );
        assert!(!serialized.to_string().contains("evt_private"));
        assert!(!serialized.to_string().contains("private_neighbor"));
        let evidence = run.evidence_id();
        assert!(
            service
                .replayable(
                    "tenant_a", "owner", &context, "entity_a", &evidence
                )
                .await?
        );
        assert!(
            !service
                .replayable(
                    "tenant_a",
                    "owner",
                    &context,
                    "entity_a",
                    "run_a:old"
                )
                .await?
        );
        assert!(
            !service
                .replayable(
                    "tenant_a",
                    "root_reader",
                    &context,
                    "entity_a",
                    &evidence
                )
                .await?
        );
        assert!(
            service
                .latest("tenant_b", "owner", &context, "entity_a")
                .await?
                .is_none()
        );
        Ok(())
    }
}
