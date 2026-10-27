//! Evidence becomes model input only after live authorization of its canonical
//! event.

use std::collections::{HashMap, HashSet};
use std::sync::Weak;

use anyhow::{Context, Result, ensure};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::authorization::{Permission, QueryContext};
use super::search::SearchService;
use crate::application::ports::conversation_agent::{
    ConversationAgent, DocumentOperation,
};
use crate::domain::evidence::{EvidenceFilter, EvidencePage};

pub struct EvidenceRequest<'a> {
    pub filter: EvidenceFilter,
    pub query: Option<&'a str>,
    pub limit: usize,
    pub after: Option<&'a str>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Cursor {
    scope: String,
    offset: u32,
}

#[derive(Deserialize)]
struct Ranking {
    passages: Vec<Ranked>,
    engine: String,
    #[serde(default)]
    warnings: Vec<String>,
}
#[derive(Deserialize)]
struct Ranked {
    id: String,
    score: f64,
}

impl SearchService {
    /// A weak reference avoids the Scribe -> tools -> search ownership cycle.
    pub fn set_evidence_runtime(
        &self,
        agent: Weak<dyn ConversationAgent>,
    ) -> Result<()> {
        self.memory.set(agent).map_err(|_| {
            anyhow::anyhow!("Evidence runtime already configured")
        })
    }

    #[tracing::instrument(skip_all)]
    pub async fn evidence(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        request: EvidenceRequest<'_>,
    ) -> Result<EvidencePage> {
        let EvidenceRequest {
            filter,
            query,
            limit,
            after,
        } = request;
        let filter = filter.validated()?;
        let query = query.map(str::trim).filter(|value| !value.is_empty());
        ensure!(
            (1..=50).contains(&limit) &&
                query.is_none_or(|text| text.len() <= 2048),
            "Invalid evidence bounds"
        );
        let scope = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&(
                tenant, user, &filter, query
            ))?)
        );
        let mut offset = match after {
            None => 0,
            Some(value) => {
                ensure!(value.len() <= 1024, "Invalid evidence cursor");
                let cursor: Cursor =
                    serde_json::from_slice(&URL_SAFE_NO_PAD.decode(value)?)?;
                ensure!(
                    cursor.scope == scope && cursor.offset <= 1_000_000,
                    "Invalid evidence cursor"
                );
                cursor.offset
            },
        };
        for (kind, selector) in [
            ("pengolo_file", filter.document_id.as_deref()),
            ("ontology", filter.ontology_id.as_deref()),
        ] {
            if let Some(id) = selector {
                ensure!(
                    self.auth
                        .is_authorized(
                            user,
                            tenant,
                            Permission::View,
                            kind,
                            id,
                            Some(context)
                        )
                        .await?,
                    "Evidence selector unavailable"
                );
            }
        }
        let mut items = Vec::with_capacity(limit);
        let mut scanned = 0usize;
        let mut exhausted = false;
        while items.len() < limit && scanned < 256 {
            let candidates = self
                .store
                .evidence_page(tenant, &filter, query, offset, 32)
                .await?;
            if candidates.is_empty() {
                exhausted = true;
                break;
            }
            ensure!(candidates.len() <= 32, "Invalid evidence batch");
            let batch_len = candidates.len();
            let mut consumed = 0usize;
            for row in candidates {
                offset = offset
                    .checked_add(1)
                    .context("Evidence cursor overflow")?;
                scanned += 1;
                consumed += 1;
                if self
                    .auth
                    .is_authorized(
                        user,
                        tenant,
                        Permission::View,
                        "event",
                        &row.event_id,
                        Some(context),
                    )
                    .await?
                {
                    items.push(row);
                }
                if items.len() == limit || scanned == 256 {
                    break;
                }
            }
            if batch_len < 32 && consumed == batch_len {
                exhausted = true;
                break;
            }
        }
        let mut engine = "chronology".to_owned();
        let mut warnings = Vec::new();
        if let Some(query) = query &&
            !items.is_empty()
        {
            let agent = self
                .memory
                .get()
                .and_then(Weak::upgrade)
                .context("Evidence ranking unavailable")?;
            let passages:Vec<Value>=items.iter().map(|row|json!({"id":row.unit_id,"text":row.text,"dense_score":0.,"facts":row.facts})).collect();
            let facts: Vec<Value> = items.iter().flat_map(|row|row.facts.iter().enumerate().map(|(ordinal,fact)|json!({"passage_id":row.unit_id,"ordinal":ordinal,"fact":fact,"similarity":0.}))).take(64).collect();
            let ranked:Ranking=serde_json::from_value(agent.document_operation(DocumentOperation::Rank,json!({"query":query,"passages":passages,"facts":facts,"strategy":"hipporag2","min_relevance":0.})).await?)?;
            ensure!(
                ranked.passages.len() <= items.len() &&
                    ranked.warnings.len() <= 8 &&
                    ranked.engine.len() <= 128,
                "Invalid evidence ranking"
            );
            let mut seen = HashSet::new();
            let mut scores = HashMap::with_capacity(ranked.passages.len());
            for hit in ranked.passages {
                ensure!(
                    hit.score.is_finite() &&
                        seen.insert(hit.id.clone()) &&
                        items.iter().any(|row| row.unit_id == hit.id),
                    "Unbound evidence ranking"
                );
                scores.insert(hit.id, hit.score);
            }
            items.retain_mut(|row| {
                if let Some(score) = scores.get(&row.unit_id) {
                    row.score = *score;
                    true
                } else {
                    false
                }
            });
            items.sort_by(|a, b| {
                b.score
                    .total_cmp(&a.score)
                    .then_with(|| a.unit_id.cmp(&b.unit_id))
            });
            engine = ranked.engine;
            warnings = ranked.warnings;
            let mut current = Vec::with_capacity(items.len());
            for row in items {
                if self
                    .auth
                    .is_authorized(
                        user,
                        tenant,
                        Permission::View,
                        "event",
                        &row.event_id,
                        Some(context),
                    )
                    .await?
                {
                    current.push(row);
                }
            }
            items = current;
        }
        tracing::debug!(
            event.name = "evidence.query.completed",
            returned = items.len(),
            scanned,
            ranked = query.is_some()
        );
        let after = if exhausted || offset > 1_000_000 {
            None
        } else {
            Some(
                URL_SAFE_NO_PAD
                    .encode(serde_json::to_vec(&Cursor { scope, offset })?),
            )
        };
        Ok(EvidencePage {
            items,
            after,
            engine,
            warnings,
        })
    }
}
