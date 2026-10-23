//! Retrieval ranks derived evidence while the workspace owns authorization.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use base64::Engine as _;
use futures::StreamExt as _;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::application::ports::conversation_agent::{
    ConversationAgent, DocumentOperation,
};
use crate::application::ports::document_index::{
    DocumentIndex, GraphLink, IndexJob, IndexScope,
};
use crate::application::ports::pengolo_store::PengoloObjects;
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::domain::document_retrieval::{
    Feature, IndexStage, SearchInput, SearchMode, SearchPage, Unit,
};

pub struct DocumentRetrieval {
    pub index: Arc<dyn DocumentIndex>,
    agent: Arc<dyn ConversationAgent>,
    objects: Arc<dyn PengoloObjects>,
    authorization: Arc<dyn Authorization>,
}

/// Keeps polling indexing transactions while renewal waits for their locks.
async fn with_lease_renewal<T, F, R>(
    operation: impl Future<Output = Result<T>>,
    mut renew: F,
) -> Result<T>
where
    F: FnMut() -> R,
    R: Future<Output = Result<()>>,
{
    let mut heartbeat =
        tokio::time::interval(std::time::Duration::from_secs(30));
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let renewal = async {
        loop {
            heartbeat.tick().await;
            if let Err(error) = renew().await {
                break Err(error);
            }
        }
    };
    tokio::select! {
        result = operation => result,
        result = renewal => result,
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Parsed {
    parser_revision: String,
    units: Vec<ParsedUnit>,
    warnings: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ParsedUnit {
    ordinal: i32,
    text: String,
    start: i64,
    end: i64,
    page: Option<i32>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Embedded {
    profile_id: String,
    vectors: Vec<Vec<f32>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Extracted {
    passages: Vec<ExtractedUnit>,
    #[serde(default)]
    warnings: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExtractedUnit {
    id: String,
    facts: Vec<crate::domain::document_retrieval::Fact>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Ranked {
    passages: Vec<RankedUnit>,
    warnings: Vec<String>,
    reranker_profile: Option<String>,
    #[serde(default)]
    engine: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RankedUnit {
    id: String,
    score: f64,
    graph_score: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Recognized {
    indices: Vec<usize>,
    warnings: Vec<String>,
}

fn unit_id(
    source: &str,
    checksum: &str,
    page: i32,
    start: i64,
    text: &str,
) -> String {
    let mut hash = Sha256::new();
    for part in [
        source.as_bytes(),
        checksum.as_bytes(),
        &page.to_le_bytes(),
        &start.to_le_bytes(),
        text.as_bytes(),
    ] {
        hash.update(part.len().to_le_bytes());
        hash.update(part);
    }
    format!("{:x}", hash.finalize())
}

fn fuse(lexical: &[String], dense: &[String]) -> HashMap<String, f64> {
    let mut scores = HashMap::with_capacity(lexical.len() + dense.len());
    for branch in [lexical, dense] {
        let mut seen = HashSet::with_capacity(branch.len());
        for (rank, id) in branch.iter().enumerate() {
            if seen.insert(id) {
                *scores.entry(id.clone()).or_insert(0.) +=
                    1. / (61. + rank as f64);
            }
        }
    }
    scores
}

fn valid_ranking(
    ranked: &Ranked,
    hits: &[crate::domain::document_retrieval::SearchHit],
) -> bool {
    let mut seen = HashSet::with_capacity(ranked.passages.len().min(64));
    ranked.passages.len() <= 64 &&
        ranked.warnings.len() <= 8 &&
        ranked.passages.iter().all(|item| {
            seen.insert(item.id.as_str()) &&
                item.score.is_finite() &&
                (0.0..=1.0).contains(&item.score) &&
                item.graph_score.is_none_or(|score| {
                    score.is_finite() && (0.0..=1.0).contains(&score)
                }) &&
                hits.iter().any(|hit| hit.unit_id == item.id)
        })
}

async fn authorized_graph_links(
    auth: &dyn Authorization,
    tenant: &str,
    owner: &str,
    context: &QueryContext,
    links: Vec<GraphLink>,
) -> Result<Vec<GraphLink>> {
    let mut visible = Vec::with_capacity(links.len());
    for link in links {
        let policy = QueryContext {
            entity_id: Some(link.entity_id.clone()),
            state_type: link.state_type.clone(),
            ..context.clone()
        };
        if auth
            .is_authorized(
                owner,
                tenant,
                Permission::View,
                "entity_state",
                &link.entity_id,
                Some(&policy),
            )
            .await? &&
            auth.is_authorized(
                owner,
                tenant,
                Permission::View,
                "event",
                &link.event_id,
                Some(&policy),
            )
            .await?
        {
            visible.push(link);
        }
    }
    Ok(visible)
}

fn graph_scope_matches(original: &[GraphLink], current: &[GraphLink]) -> bool {
    original.iter().all(|original| {
        current.iter().any(|link| {
            link.passage_id == original.passage_id &&
                link.entity_id == original.entity_id &&
                link.event_id == original.event_id &&
                link.state_type == original.state_type
        })
    })
}

impl DocumentRetrieval {
    async fn graph_view(
        &self,
        scope: IndexScope<'_>,
        context: &QueryContext,
        hits: &mut Vec<crate::domain::document_retrieval::SearchHit>,
    ) -> Result<Vec<GraphLink>> {
        let units: Vec<_> = hits
            .iter()
            .take(256)
            .map(|hit| hit.unit_id.clone())
            .collect();
        let links = authorized_graph_links(
            self.authorization.as_ref(),
            scope.tenant,
            scope.owner,
            context,
            self.index.graph_links(scope, &units).await?,
        )
        .await?;
        if links.is_empty() {
            return Ok(links);
        }
        let expanded = self.index.graph_expand(scope, &links).await?;
        let extra_units: Vec<_> =
            expanded.iter().map(|hit| hit.unit_id.clone()).collect();
        let extra_links = authorized_graph_links(
            self.authorization.as_ref(),
            scope.tenant,
            scope.owner,
            context,
            self.index.graph_links(scope, &extra_units).await?,
        )
        .await?;
        let linked: HashSet<_> = extra_links
            .iter()
            .filter(|link| {
                links.iter().any(|seed| {
                    seed.entity_id == link.entity_id ||
                        seed.event_id == link.event_id
                })
            })
            .map(|link| link.passage_id.as_str())
            .collect();
        let added: Vec<_> = expanded
            .into_iter()
            .filter(|hit| {
                linked.contains(hit.unit_id.as_str()) &&
                    !hits
                        .iter()
                        .any(|existing| existing.unit_id == hit.unit_id)
            })
            .collect();
        ensure!(
            hits.len() + added.len() <= 256,
            "Graph candidate budget exceeded"
        );
        let units: HashSet<_> = hits
            .iter()
            .chain(added.iter())
            .map(|hit| hit.unit_id.as_str())
            .collect();
        let mut view = links;
        for link in extra_links {
            if units.contains(link.passage_id.as_str()) &&
                !view.iter().any(|existing| {
                    existing.passage_id == link.passage_id &&
                        existing.entity_id == link.entity_id &&
                        existing.event_id == link.event_id
                })
            {
                ensure!(view.len() < 1024, "Graph link budget exceeded");
                view.push(link);
            }
        }
        hits.extend(added);
        Ok(view)
    }

    pub fn new(
        index: Arc<dyn DocumentIndex>,
        agent: Arc<dyn ConversationAgent>,
        objects: Arc<dyn PengoloObjects>,
        authorization: Arc<dyn Authorization>,
    ) -> Self {
        Self {
            index,
            agent,
            objects,
            authorization,
        }
    }

    async fn embeddings(
        &self,
        texts: Vec<&str>,
        query: bool,
    ) -> Result<Embedded> {
        let expected = texts.len();
        let value = self
            .agent
            .document_operation(
                DocumentOperation::Embed,
                json!({"texts":texts,"query":query}),
            )
            .await?;
        let result: Embedded = serde_json::from_value(value)?;
        ensure!(
            result.profile_id.len() == 64 &&
                result
                    .profile_id
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit()) &&
                result.vectors.len() == expected,
            "Invalid embedding profile or batch"
        );
        let dimension = result.vectors.first().map_or(0, Vec::len);
        ensure!(
            (1..=4096).contains(&dimension) &&
                result.vectors.iter().all(|vector| vector.len() ==
                    dimension &&
                    vector.iter().all(|value| value.is_finite()) &&
                    vector
                        .iter()
                        .map(|value| f64::from(*value).powi(2))
                        .sum::<f64>() >
                        1e-12),
            "Invalid embedding vectors"
        );
        Ok(result)
    }

    #[tracing::instrument(skip_all)]
    pub async fn search(
        &self,
        tenant: &str,
        owner: &str,
        input: &SearchInput,
        context: &QueryContext,
    ) -> Result<SearchPage> {
        let mut warnings = Vec::new();
        let embedding = if input.mode == SearchMode::Lexical {
            None
        } else {
            match self.embeddings(vec![&input.query], true).await {
                Ok(value) => Some(value),
                Err(_) => {
                    warnings.push("embeddings_unavailable".into());
                    None
                },
            }
        };
        let profile = embedding
            .as_ref()
            .map(|embedding| embedding.profile_id.as_str());
        let vector = embedding
            .as_ref()
            .and_then(|embedding| embedding.vectors.first())
            .map(Vec::as_slice);
        let mut effective = input.clone();
        if vector.is_none() && input.mode == SearchMode::Semantic {
            effective.mode = SearchMode::Lexical;
        }
        let mut candidates = self
            .index
            .candidates(
                IndexScope {
                    tenant,
                    owner,
                    input: &effective,
                },
                vector,
                profile,
            )
            .await?;
        let mut recognized = Vec::new();
        if input.mode == SearchMode::Hipporag2 && !candidates.facts.is_empty()
        {
            match self
                .agent
                .document_operation(
                    DocumentOperation::Recognize,
                    json!({"query":input.query,"facts":candidates.facts}),
                )
                .await
                .and_then(|value| {
                    Ok(serde_json::from_value::<Recognized>(value)?)
                }) {
                Ok(selected)
                    if selected.indices.len() <= 5 &&
                        selected.warnings.len() <= 8 &&
                        selected.indices.iter().all(|index| {
                            *index < candidates.facts.len()
                        }) =>
                {
                    warnings.extend(selected.warnings);
                    let mut seen = HashSet::new();
                    recognized = selected
                        .indices
                        .into_iter()
                        .filter(|index| seen.insert(*index))
                        .collect();
                    let seeds: Vec<_> = recognized
                        .iter()
                        .filter_map(|index| {
                            candidates.facts.get(*index).cloned()
                        })
                        .collect();
                    let expanded = match self
                        .index
                        .expand(
                            IndexScope {
                                tenant,
                                owner,
                                input: &effective,
                            },
                            &seeds,
                        )
                        .await
                    {
                        Ok(hits) => hits,
                        Err(_) => {
                            warnings
                                .push("graph_expansion_unavailable".into());
                            Vec::new()
                        },
                    };
                    for hit in expanded {
                        if !candidates
                            .hits
                            .iter()
                            .any(|existing| existing.unit_id == hit.unit_id)
                        {
                            candidates.hits.push(hit);
                        }
                    }
                },
                _ => warnings.push("recognition_unavailable".into()),
            }
        }
        let fusion = fuse(&candidates.lexical, &candidates.dense);
        for hit in &mut candidates.hits {
            hit.rank_score = fusion.get(&hit.unit_id).copied().unwrap_or(0.);
        }
        candidates.hits.sort_by(|left, right| {
            right
                .rank_score
                .total_cmp(&left.rank_score)
                .then_with(|| left.unit_id.cmp(&right.unit_id))
        });
        let mut reranker_profile = None;
        let mut engine = "first-stage".to_owned();
        let mut graph_links = Vec::new();
        let initial_candidates = candidates.hits.len();
        if input.mode == SearchMode::Hipporag2 && input.include_eskg {
            match self
                .graph_view(
                    IndexScope {
                        tenant,
                        owner,
                        input: &effective,
                    },
                    context,
                    &mut candidates.hits,
                )
                .await
            {
                Ok(links) => graph_links = links,
                Err(_) => warnings.push("eskg_links_unavailable".into()),
            }
        }
        if !candidates.hits.is_empty() &&
            matches!(input.mode, SearchMode::Hybrid | SearchMode::Hipporag2)
        {
            let passages: Vec<Value> = candidates.hits.iter().take(256).map(|hit|json!({"id":hit.unit_id,"text":hit.text,"dense_score":hit.similarity.unwrap_or(0.),"facts":hit.facts})).collect();
            let mut ranking = self.agent.document_operation(DocumentOperation::Rank, json!({"query":input.query,"passages":passages,"facts":candidates.facts,"strategy":if input.mode==SearchMode::Hipporag2 {"hipporag2"} else {"hybrid"},"min_relevance":input.min_relevance,"recognized":{"indices":recognized},"eskg_links":graph_links})).await;
            if !graph_links.is_empty() {
                let units: Vec<_> = candidates
                    .hits
                    .iter()
                    .map(|hit| hit.unit_id.clone())
                    .collect();
                let current = match self
                    .index
                    .graph_links(
                        IndexScope {
                            tenant,
                            owner,
                            input: &effective,
                        },
                        &units,
                    )
                    .await
                {
                    Ok(links) => {
                        let result = authorized_graph_links(
                            self.authorization.as_ref(),
                            tenant,
                            owner,
                            context,
                            links,
                        )
                        .await;
                        if result.is_err() {
                            tracing::warn!(
                                event.name = "document.graph.revalidation",
                                failure = "authorization_unavailable"
                            );
                        }
                        result
                    },
                    Err(error) => {
                        tracing::warn!(
                            event.name = "document.graph.revalidation",
                            failure = "index_unavailable",
                            candidates = units.len()
                        );
                        Err(error)
                    },
                };
                if !current.as_ref().is_ok_and(|links| {
                    graph_scope_matches(&graph_links, links)
                }) {
                    tracing::warn!(
                        event.name = "document.graph.scope_changed",
                        original_links = graph_links.len(),
                        current_links = current.as_ref().ok().map(Vec::len),
                        candidates = units.len()
                    );
                    ranking =
                        Err(anyhow::anyhow!("Graph evidence scope changed"));
                    graph_links.clear();
                    candidates.hits.truncate(initial_candidates);
                    warnings.push("eskg_scope_changed".into());
                }
            }
            match ranking
                .and_then(|value| Ok(serde_json::from_value::<Ranked>(value)?))
            {
                Ok(ranked) if valid_ranking(&ranked, &candidates.hits) => {
                    let mut selected =
                        Vec::with_capacity(ranked.passages.len());
                    let mut available: HashMap<_, _> =
                        std::mem::take(&mut candidates.hits)
                            .into_iter()
                            .map(|hit| (hit.unit_id.clone(), hit))
                            .collect();
                    for item in ranked.passages {
                        let mut hit = available
                            .remove(&item.id)
                            .context("Ranking introduced unknown evidence")?;
                        hit.relevance = ranked
                            .reranker_profile
                            .as_ref()
                            .map(|_| item.score);
                        hit.graph_score = item.graph_score;
                        selected.push(hit);
                    }
                    candidates.hits = selected;
                    warnings.extend(ranked.warnings);
                    reranker_profile = ranked.reranker_profile;
                    engine = ranked.engine.unwrap_or_else(|| "legacy".into());
                },
                _ => warnings.push("ranking_unavailable".into()),
            }
        }
        let mut counts = HashMap::<String, usize>::new();
        candidates.hits.retain(|hit| {
            let count = counts.entry(hit.source_id.clone()).or_default();
            *count += 1;
            *count <= 2
        });
        candidates.hits.truncate(input.limit);
        for hit in &mut candidates.hits {
            hit.eskg_refs = graph_links
                .iter()
                .filter(|link| link.passage_id == hit.unit_id)
                .take(16)
                .map(|link| {
                    crate::domain::document_retrieval::GraphReference {
                        entity_id: link.entity_id.clone(),
                        event_id: link.event_id.clone(),
                        state_type: link.state_type.clone(),
                    }
                })
                .collect();
        }
        Ok(SearchPage {
            hits: candidates.hits,
            warnings,
            lexical_backend: candidates.lexical_backend,
            embedding_profile: profile.map(str::to_owned),
            reranker_profile,
            engine,
        })
    }

    #[tracing::instrument(skip_all)]
    async fn index_job(
        &self,
        tenant: &str,
        job: &IndexJob,
        resumed: Option<Vec<Unit>>,
    ) -> Result<()> {
        let (mut indexed_units, mut warnings) = if let Some(units) = resumed {
            (units, Vec::new())
        } else {
            self.index
                .progress(tenant, job, IndexStage::Reading, 2)
                .await?;
            let key = job
                .file
                .data
                .get("key")
                .and_then(Value::as_str)
                .context("Missing source key")?;
            let mime = job
                .file
                .data
                .get("mime")
                .and_then(Value::as_str)
                .context("Missing source MIME")?;
            let mut stream = self.objects.read(key).await?;
            let mut content = Vec::new();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk?;
                ensure!(
                    content.len() + chunk.len() <= 25 * 1024 * 1024,
                    "Document indexing size exceeded"
                );
                content.extend_from_slice(&chunk);
            }
            let checksum = format!("{:x}", Sha256::digest(&content));
            let stored = base64::engine::general_purpose::STANDARD
                .decode(&job.checksum)?;
            ensure!(
                stored.as_slice() == Sha256::digest(&content).as_slice(),
                "Source checksum mismatch"
            );
            self.index
                .progress(tenant, job, IndexStage::Parsing, 10)
                .await?;
            let parsed:Parsed=serde_json::from_value(self.agent.document_operation(DocumentOperation::Parse,json!({"mime":mime,"content":base64::engine::general_purpose::STANDARD.encode(&content),"checksum":checksum})).await?)?;
            ensure!(
                parsed.units.len() <= 2048 &&
                    parsed.parser_revision.len() <= 128 &&
                    parsed.warnings.len() <= 8,
                "Invalid parsed evidence"
            );
            let warnings = parsed.warnings;
            let indexed_units: Vec<Unit> = parsed
                .units
                .into_iter()
                .map(|unit| Unit {
                    id: unit_id(
                        &job.source_id,
                        &job.checksum,
                        unit.page.unwrap_or(0),
                        unit.start,
                        &unit.text,
                    ),
                    ordinal: unit.ordinal,
                    text: unit.text,
                    start: unit.start,
                    end: unit.end,
                    page: unit.page,
                    facts: Vec::new(),
                })
                .collect();
            (indexed_units, warnings)
        };
        let mut profile = job.profile.clone();
        let mut lexical_only = false;
        let total_units = indexed_units.len().max(1);
        indexed_units.retain(|unit| !job.completed.contains(&unit.id));
        let resumed_units = total_units.saturating_sub(indexed_units.len());
        tracing::info!(
            event.name = "document.index.resumed",
            completed = resumed_units,
            pending = indexed_units.len()
        );
        // Publish bounded lexical evidence before cold model loading can delay
        // enrichment.
        for batch in indexed_units.chunks(32) {
            self.index.stage(tenant, job, batch, &[], None).await?;
        }
        if !indexed_units.is_empty() {
            let mut pending = warnings.clone();
            pending.truncate(7);
            pending.push("enrichment_pending".into());
            self.index.checkpoint(tenant, job, &pending).await?;
        }
        // Integer estimates bound progress writes independently of chunk
        // count.
        let mut reported = 10;
        for (batch, units) in indexed_units.chunks_mut(1).enumerate() {
            let ordinal = resumed_units + batch;
            let mut extraction_complete = false;
            let percent = u8::try_from(15 + 80 * ordinal / total_units)?;
            if percent > reported {
                self.index
                    .progress(tenant, job, IndexStage::Extracting, percent)
                    .await?;
                reported = percent;
            }
            let passages: Vec<Value> = units
                .iter()
                .map(|unit| json!({"id":unit.id,"text":unit.text}))
                .collect();
            match self
                .agent
                .document_operation(
                    DocumentOperation::Extract,
                    json!({"passages":passages}),
                )
                .await
                .and_then(|value| {
                    Ok(serde_json::from_value::<Extracted>(value)?)
                }) {
                Ok(extracted) => {
                    ensure!(
                        extracted.passages.len() <= units.len(),
                        "Invalid extraction count"
                    );
                    ensure!(
                        extracted.warnings.len() <= 8,
                        "Extraction warnings exceed limit"
                    );
                    extraction_complete = extracted.passages.len() ==
                        units.len() &&
                        !extracted.warnings.iter().any(|warning| {
                            warning.ends_with("_unavailable") ||
                                warning.ends_with("_unconfigured")
                        });
                    for warning in extracted.warnings {
                        if !warnings.contains(&warning) {
                            warnings.push(warning);
                        }
                    }
                    let mut seen = HashSet::new();
                    for item in extracted.passages {
                        ensure!(
                            seen.insert(item.id.clone()),
                            "Duplicate extraction evidence"
                        );
                        units
                            .iter_mut()
                            .find(|unit| unit.id == item.id)
                            .context("Unknown extraction evidence")?
                            .facts = item.facts;
                    }
                },
                Err(_) => {
                    if !warnings
                        .iter()
                        .any(|warning| warning == "extraction_unavailable")
                    {
                        warnings.push("extraction_unavailable".into());
                    }
                },
            }
            let mut features = Vec::new();
            if !lexical_only {
                let percent =
                    u8::try_from(15 + (80 * ordinal + 40) / total_units)?;
                if percent > reported {
                    self.index
                        .progress(tenant, job, IndexStage::Embedding, percent)
                        .await?;
                    reported = percent;
                }
                let mut texts: Vec<String> =
                    Vec::with_capacity(units.len() * 9);
                let mut positions = Vec::with_capacity(units.len() * 9);
                for unit in units.iter() {
                    texts.push(unit.text.clone());
                    positions.push((unit.id.as_str(), -1));
                    for (ordinal, fact) in unit.facts.iter().enumerate() {
                        texts.push(format!(
                            "{} {} {}",
                            fact.subject, fact.predicate, fact.object
                        ));
                        positions
                            .push((unit.id.as_str(), i32::try_from(ordinal)?));
                    }
                }
                for (values, positions) in
                    texts.chunks(32).zip(positions.chunks(32))
                {
                    match self
                        .embeddings(
                            values.iter().map(String::as_str).collect(),
                            false,
                        )
                        .await
                    {
                        Ok(embedded) => {
                            ensure!(
                                profile
                                    .as_ref()
                                    .is_none_or(|profile| profile ==
                                        &embedded.profile_id),
                                "Embedding profile changed during indexing"
                            );
                            profile = Some(embedded.profile_id);
                            features.extend(
                                positions.iter().zip(embedded.vectors).map(
                                    |((id, ordinal), embedding)| Feature {
                                        unit_id: (*id).into(),
                                        ordinal: *ordinal,
                                        embedding,
                                    },
                                ),
                            );
                        },
                        Err(_) => {
                            lexical_only = true;
                            warnings.push("embeddings_unavailable".into());
                            break;
                        },
                    }
                }
            }
            self.index
                .stage(tenant, job, units, &features, profile.as_deref())
                .await?;
            if extraction_complete && !lexical_only {
                self.index.complete(tenant, job, units).await?;
            }
        }
        warnings.truncate(8);
        self.index
            .progress(tenant, job, IndexStage::Publishing, 95)
            .await?;
        self.index
            .publish(tenant, job, profile.as_deref(), &warnings)
            .await
    }

    pub async fn tick(&self) -> Result<()> {
        for tenant in self.index.tenants_with_work().await? {
            if let Some(mut job) = self.index.claim(&tenant).await? {
                let resumed = job.units.take();
                let indexing = with_lease_renewal(
                    self.index_job(&tenant, &job, resumed),
                    || self.index.renew(&tenant, &job),
                );
                let result = tokio::time::timeout(
                    std::time::Duration::from_secs(900),
                    indexing,
                )
                .await;
                if !matches!(result, Ok(Ok(()))) {
                    tracing::warn!(
                        event.name = "document.index.interrupted",
                        timeout = result.is_err()
                    );
                    self.index.fail(&tenant, &job, "unavailable").await?;
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #[tokio::test]
    async fn lease_renewal_failure_cancels_indexing_and_releases_its_lock()
    -> anyhow::Result<()> {
        let lock = tokio::sync::Mutex::new(());
        let locked = tokio::sync::Notify::new();
        let operation = async {
            let _guard = lock.lock().await;
            locked.notify_one();
            std::future::pending::<anyhow::Result<()>>().await
        };
        let renew = || async {
            locked.notified().await;
            anyhow::bail!("Lease revoked")
        };
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            super::with_lease_renewal(operation, renew),
        )
        .await?;
        assert_eq!(
            result.err().map(|error| error.to_string()).as_deref(),
            Some("Lease revoked")
        );
        assert!(lock.try_lock().is_ok());
        Ok(())
    }

    #[tokio::test]
    async fn lease_renewal_keeps_polling_the_transaction_that_owns_its_lock()
    -> anyhow::Result<()> {
        let lock = tokio::sync::Mutex::new(());
        let locked = tokio::sync::Notify::new();
        let renewing = tokio::sync::Notify::new();
        let operation = async {
            let guard = lock.lock().await;
            locked.notify_one();
            renewing.notified().await;
            tokio::task::yield_now().await;
            drop(guard);
            Ok(())
        };
        let renew = || async {
            locked.notified().await;
            renewing.notify_one();
            let _guard = lock.lock().await;
            Ok(())
        };
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            super::with_lease_renewal(operation, renew),
        )
        .await??;
        Ok(())
    }

    struct RestrictedGraph;

    #[async_trait::async_trait]
    impl crate::application::usecases::authorization::Authorization
        for RestrictedGraph
    {
        async fn upsert_relationship(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> anyhow::Result<()> {
            anyhow::bail!("Writes are not expected")
        }

        async fn delete_relationship(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> anyhow::Result<()> {
            anyhow::bail!("Writes are not expected")
        }

        async fn is_authorized(
            &self,
            _: &str,
            _: &str,
            _: crate::application::usecases::authorization::Permission,
            resource: &str,
            id: &str,
            context: Option<
                &crate::application::usecases::authorization::QueryContext,
            >,
        ) -> anyhow::Result<bool> {
            let context = context
                .ok_or_else(|| anyhow::anyhow!("Missing policy context"))?;
            Ok(context.state_type.as_deref() == Some("legal") &&
                (resource == "entity_state" && id == "visible" ||
                    resource == "event" && id == "allowed"))
        }

        async fn invalidate_tenant_cache(&self, _: &str) {}
    }

    #[tokio::test]
    async fn graph_links_require_independent_entity_and_event_authorization()
    -> anyhow::Result<()> {
        use crate::application::ports::document_index::GraphLink;
        use crate::application::usecases::authorization::QueryContext;
        let links = vec![
            GraphLink {
                passage_id: "a".repeat(64),
                entity_id: "visible".into(),
                event_id: "allowed".into(),
                state_type: Some("legal".into()),
            },
            GraphLink {
                passage_id: "b".repeat(64),
                entity_id: "visible".into(),
                event_id: "forbidden".into(),
                state_type: Some("legal".into()),
            },
            GraphLink {
                passage_id: "c".repeat(64),
                entity_id: "hidden".into(),
                event_id: "allowed".into(),
                state_type: Some("legal".into()),
            },
        ];
        let selected = super::authorized_graph_links(
            &RestrictedGraph,
            "tenant",
            "reader",
            &QueryContext::default(),
            links,
        )
        .await?;
        assert_eq!(selected.len(), 1);
        assert_eq!(
            selected.first().map(|link| link.passage_id.as_str()),
            Some("a".repeat(64).as_str())
        );
        Ok(())
    }
    use super::*;

    #[test]
    fn graph_scope_revalidation_preserves_policy_context() {
        let original = GraphLink {
            passage_id: "passage".into(),
            entity_id: "entity".into(),
            event_id: "event".into(),
            state_type: Some("legal".into()),
        };
        assert!(graph_scope_matches(
            std::slice::from_ref(&original),
            std::slice::from_ref(&original)
        ));
        let mut changed = original.clone();
        changed.state_type = Some("restricted".into());
        assert!(!graph_scope_matches(
            std::slice::from_ref(&original),
            &[changed]
        ));
        assert!(!graph_scope_matches(&[original], &[]));
    }

    #[test]
    fn fusion_uses_ranks_instead_of_incompatible_backend_scores() {
        let fused = fuse(&["a".into(), "b".into()], &["b".into(), "c".into()]);
        assert!(fused.get("b") > fused.get("a"));
        assert_eq!(fused.len(), 3);
    }

    #[test]
    fn evidence_identity_tracks_the_source_revision_and_location() {
        assert_ne!(
            unit_id("file", "checksum-a", 0, 10, "proof"),
            unit_id("file", "checksum-b", 0, 10, "proof")
        );
        assert_ne!(
            unit_id("file", "checksum-a", 0, 10, "proof"),
            unit_id("file", "checksum-a", 1, 11, "proof")
        );
        assert_ne!(
            unit_id("file", "checksum", 1, 0, "first"),
            unit_id("file", "checksum", 1, 0, "other")
        );
    }
}
#[test]
fn unknown_ranked_evidence_is_rejected_before_mutating_candidates()
-> Result<()> {
    let ranked: Ranked = serde_json::from_value(
        json!({"passages":[{"id":"invented","score":0.8,"graph_score":null}],"warnings":[],"reranker_profile":null}),
    )?;
    assert!(!valid_ranking(&ranked, &[]));
    Ok(())
}
