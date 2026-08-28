//! Cross-domain search use cases.
//!
//! Security model:
//! - DB queries are tenant_id constrained.
//! - Final authorization is enforced via SpiceDB/Loth per entity_id using
//!   resource type `entity_state`.

use std::sync::Arc;

use anyhow::{Context, Result, bail};
use serde_json::Value;

use crate::application::ports::embedding_generator::EmbeddingGenerator;
use crate::application::ports::entity_state_store::EntityStateStore;
use crate::application::ports::search_store::{
    EmbeddingRow, EventRow, SearchStore,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};

const HARD_LIMIT: usize = 50;

#[derive(Debug, Clone, PartialEq)]
pub enum GlobalSearchHit {
    EntityState {
        entity_id: String,
        state: Value,
    },
    Event {
        event_id: String,
        event_type: String,
        event_time_ms: i64,
        properties: Value,
    },
    Embedding {
        entity_id: String,
        modality: String,
        created_at_ms: i64,
        metadata: Value,
        score: f32,
    },
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct StructuredSearchQuery<'a> {
    pub text: Option<&'a str>,
    pub entity_id: Option<&'a str>,
    pub event_type: Option<&'a str>,
    pub modality: Option<&'a str>,
}

impl<'a> StructuredSearchQuery<'a> {
    /// Trims optional filters and requires at least one retrieval constraint.
    fn normalized(self) -> Result<Self> {
        /// Removes blank optional string filters without allocating.
        fn present(value: Option<&str>) -> Option<&str> {
            value.map(str::trim).filter(|value| !value.is_empty())
        }

        let normalized = Self {
            text: present(self.text),
            entity_id: present(self.entity_id),
            event_type: present(self.event_type),
            modality: present(self.modality),
        };
        if normalized.text.is_none() &&
            normalized.entity_id.is_none() &&
            normalized.event_type.is_none()
        {
            bail!("structured search requires text, entity_id, or event_type");
        }
        Ok(normalized)
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct ParsedQuery<'a> {
    free_text: &'a str,
    entity_id: Option<&'a str>,
    event_type: Option<&'a str>,
    modality: Option<&'a str>,
}

/// Parses the legacy compact filter syntax into structured search fields.
fn parse_global_query(q: &str) -> ParsedQuery<'_> {
    // Tokens are `key:value` separated by whitespace.
    let mut out = ParsedQuery {
        free_text: q.trim(),
        ..Default::default()
    };

    // Fast path: if no ':' exists, treat as plain text.
    if !out.free_text.contains(':') {
        return out;
    }

    let mut first_filter_start = None;
    let mut first_free_start = None;
    let mut cursor = 0usize;

    // Scan tokens by whitespace; keep the first occurrence of known keys.
    for part in out.free_text.split_whitespace() {
        let Some(relative_start) = out.free_text[cursor..].find(part) else {
            break;
        };
        let part_start = cursor + relative_start;
        cursor = part_start + part.len();
        let recognized = if let Some((k, v)) = part.split_once(':') {
            match k {
                "entity_id" if out.entity_id.is_none() => {
                    out.entity_id = Some(v);
                    true
                },
                "event" if out.event_type.is_none() => {
                    out.event_type = Some(v);
                    true
                },
                "modality" if out.modality.is_none() => {
                    out.modality = Some(v);
                    true
                },
                _ => false,
            }
        } else {
            false
        };
        if recognized {
            first_filter_start.get_or_insert(part_start);
        } else {
            first_free_start.get_or_insert(part_start);
        }
    }

    if let Some(free_start) = first_free_start {
        out.free_text = match first_filter_start {
            Some(filter_start) if free_start < filter_start => {
                out.free_text[free_start..filter_start].trim()
            },
            _ => out.free_text[free_start..].trim(),
        };
    } else if first_filter_start.is_some() {
        out.free_text = "";
    }

    out
}

pub struct SearchService {
    states: Arc<dyn EntityStateStore>,
    store: Arc<dyn SearchStore>,
    embed: Arc<dyn EmbeddingGenerator>,
    auth: Arc<dyn Authorization>,
}

impl SearchService {
    /// Creates the search service from tenant stores and authorization.
    pub fn new(
        states: Arc<dyn EntityStateStore>,
        store: Arc<dyn SearchStore>,
        embed: Arc<dyn EmbeddingGenerator>,
        auth: Arc<dyn Authorization>,
    ) -> Self {
        Self {
            states,
            store,
            embed,
            auth,
        }
    }

    /// Parses and executes permission-filtered global search.
    pub async fn global_search(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
        query: &str,
        limit: usize,
    ) -> Result<Vec<GlobalSearchHit>> {
        let parsed = parse_global_query(query);
        self.structured_search(
            tenant_id,
            user_id,
            policy_context,
            StructuredSearchQuery {
                text: (!parsed.free_text.is_empty())
                    .then_some(parsed.free_text),
                entity_id: parsed.entity_id,
                event_type: parsed.event_type,
                modality: parsed.modality,
            },
            limit,
        )
        .await
    }

    /// Executes bounded semantic filters and authorizes every result.
    pub async fn structured_search(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
        query: StructuredSearchQuery<'_>,
        limit: usize,
    ) -> Result<Vec<GlobalSearchHit>> {
        let query = query.normalized()?;
        let lim = limit.clamp(1, HARD_LIMIT);
        let mut out = Vec::with_capacity(lim);

        let state_rows = if let Some(entity_id) = query.entity_id {
            self.states
                .latest_states_for_entity(tenant_id, entity_id, lim)
                .await
                .context("Failed to fetch entity state history")?
        } else if let Some(text) = query.text {
            self.states
                .search_by_name(tenant_id, text, lim)
                .await
                .context("Failed to search entity states")?
        } else {
            Vec::new()
        };
        for row in state_rows {
            let authorization_context = QueryContext {
                entity_id: Some(row.entity_id.clone()),
                state_type: row.state_type.clone(),
                ..policy_context.clone()
            };
            if self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "entity_state",
                    &row.entity_id,
                    Some(&authorization_context),
                )
                .await
                .context("Failed to authorize entity state hit")?
            {
                out.push(GlobalSearchHit::EntityState {
                    entity_id: row.entity_id,
                    state: row.metadata,
                });
            }
        }

        if out.len() < lim &&
            (query.event_type.is_some() || query.text.is_some())
        {
            let events = self
                .store
                .search_events(
                    tenant_id,
                    query.event_type,
                    query.text,
                    lim.saturating_sub(out.len()),
                )
                .await
                .context("Failed to search events")?;
            for event in events {
                if self
                    .auth
                    .is_authorized(
                        user_id,
                        tenant_id,
                        Permission::View,
                        "event",
                        &event.event_id,
                        Some(policy_context),
                    )
                    .await
                    .context("Failed to authorize event hit")?
                {
                    out.push(GlobalSearchHit::Event {
                        event_id: event.event_id,
                        event_type: event.event_type,
                        event_time_ms: event.event_time_ms,
                        properties: event.properties,
                    });
                }
            }
        }

        if let Some(text) = query.text &&
            out.len() < lim
        {
            let embedding = self
                .embed
                .embed_text(text)
                .await
                .context("Failed to embed structured search text")?;
            let rows = self
                .store
                .search_embeddings_top_k(
                    tenant_id,
                    query.modality,
                    &embedding,
                    lim.saturating_sub(out.len()),
                )
                .await
                .context("Failed to search entity embeddings")?;
            self.push_authorized_embeddings(
                tenant_id,
                user_id,
                policy_context,
                rows,
                &mut out,
                lim,
            )
            .await?;
        }
        out.truncate(lim);
        Ok(out)
    }

    /// Appends embedding hits only after per-entity authorization succeeds.
    async fn push_authorized_embeddings(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
        rows: Vec<EmbeddingRow>,
        out: &mut Vec<GlobalSearchHit>,
        lim: usize,
    ) -> Result<()> {
        for r in rows {
            if out.len() >= lim {
                break;
            }

            let ctx = QueryContext {
                entity_id: Some(r.entity_id.clone()),
                modality: Some(r.modality.clone()),
                state_type: None,
                gis_zone: None,
                ..policy_context.clone()
            };

            // Authorize by entity_id using the entity_state object type.
            let ok = self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "entity_state",
                    &r.entity_id,
                    Some(&ctx),
                )
                .await
                .context("Failed to authorize embedding hit")?;

            if ok {
                out.push(GlobalSearchHit::Embedding {
                    entity_id: r.entity_id,
                    modality: r.modality,
                    created_at_ms: r.created_at_ms,
                    metadata: r.metadata,
                    score: r.score,
                });
            }
        }
        Ok(())
    }

    /// Searches embeddings while enforcing per-entity visibility.
    pub async fn search_embeddings_explicit(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
        query_text: &str,
        modality: Option<&str>,
        k: usize,
    ) -> Result<Vec<EmbeddingRow>> {
        let q = query_text.trim();
        if q.is_empty() {
            bail!("query_text is empty");
        }

        let emb = self.embed.embed_text(q).await?;
        let rows = self
            .store
            .search_embeddings_top_k(tenant_id, modality, &emb, k)
            .await?;

        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            let ctx = QueryContext {
                entity_id: Some(r.entity_id.clone()),
                modality: Some(r.modality.clone()),
                state_type: None,
                gis_zone: None,
                ..policy_context.clone()
            };
            let ok = self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "entity_state",
                    &r.entity_id,
                    Some(&ctx),
                )
                .await?;
            if ok {
                out.push(r);
            }
        }

        Ok(out)
    }

    /// Searches events while enforcing per-event visibility.
    pub async fn search_events_explicit(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
    ) -> Result<Vec<EventRow>> {
        let rows = self
            .store
            .search_events(tenant_id, event_type, text, limit)
            .await?;
        let mut authorized = Vec::with_capacity(rows.len());
        for row in rows {
            if self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "event",
                    &row.event_id,
                    Some(policy_context),
                )
                .await?
            {
                authorized.push(row);
            }
        }
        Ok(authorized)
    }
}

#[cfg(test)]
mod tests {
    use anyhow::{Result, ensure};

    use super::*;
    use crate::application::ports::embedding_generator::Embedding1024;
    use crate::application::ports::entity_state_store::EntityStateRow;
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization,
    };

    struct MemoryStates;

    #[async_trait::async_trait]
    impl EntityStateStore for MemoryStates {
        async fn search_by_name(
            &self,
            tenant_id: &str,
            _: &str,
            _: usize,
        ) -> Result<Vec<EntityStateRow>> {
            ensure!(tenant_id == "tenant_a");
            Ok(vec![EntityStateRow {
                entity_id: "state-1".to_owned(),
                metadata: serde_json::json!({"name": "alpha"}),
                state_type: Some("asset".to_owned()),
                created_at_ms: Some(1),
            }])
        }

        async fn latest_states_for_entity(
            &self,
            tenant_id: &str,
            entity_id: &str,
            _: usize,
        ) -> Result<Vec<EntityStateRow>> {
            ensure!(tenant_id == "tenant_a");
            Ok(vec![EntityStateRow {
                entity_id: entity_id.to_owned(),
                metadata: serde_json::json!({"name": "exact"}),
                state_type: Some("asset".to_owned()),
                created_at_ms: Some(1),
            }])
        }
    }

    struct MemorySearch;

    #[async_trait::async_trait]
    impl SearchStore for MemorySearch {
        async fn search_events(
            &self,
            tenant_id: &str,
            _: Option<&str>,
            _: Option<&str>,
            _: usize,
        ) -> Result<Vec<EventRow>> {
            ensure!(tenant_id == "tenant_a");
            Ok(vec![EventRow {
                event_id: "event-1".to_owned(),
                event_type: "changed".to_owned(),
                event_time_ms: 2,
                properties: serde_json::json!({"value": 2}),
            }])
        }

        async fn search_embeddings_top_k(
            &self,
            tenant_id: &str,
            _: Option<&str>,
            _: &[f32; 1024],
            _: usize,
        ) -> Result<Vec<EmbeddingRow>> {
            ensure!(tenant_id == "tenant_a");
            Ok(vec![EmbeddingRow {
                id: Some("embedding-1".to_owned()),
                entity_id: "embedding-entity-1".to_owned(),
                modality: "text".to_owned(),
                created_at_ms: 3,
                metadata: serde_json::json!({"source": "verified"}),
                score: 0.25,
            }])
        }
    }

    struct MemoryEmbedding;

    #[async_trait::async_trait]
    impl EmbeddingGenerator for MemoryEmbedding {
        async fn embed_text(&self, text: &str) -> Result<Embedding1024> {
            ensure!(!text.trim().is_empty());
            Ok([0.0; 1024])
        }
    }

    fn service(decision: AuthorizationDecision) -> SearchService {
        SearchService::new(
            Arc::new(MemoryStates),
            Arc::new(MemorySearch),
            Arc::new(MemoryEmbedding),
            TestAuthorization::new(decision),
        )
    }

    #[test]
    fn parse_global_query_extracts_tokens_conservatively() {
        let p = parse_global_query(
            "entity_id:e1 event:trigger modality:vision hello world",
        );
        assert_eq!(p.entity_id, Some("e1"));
        assert_eq!(p.event_type, Some("trigger"));
        assert_eq!(p.modality, Some("vision"));
        assert_eq!(p.free_text, "hello world");
    }

    #[test]
    fn parse_global_query_plain_text() {
        let p = parse_global_query("hello");
        assert_eq!(p.free_text, "hello");
        assert_eq!(p.entity_id, None);
    }

    #[test]
    fn structured_search_requires_a_retrieval_constraint() {
        assert!(StructuredSearchQuery::default().normalized().is_err());
        assert!(
            StructuredSearchQuery {
                modality: Some("vision"),
                ..StructuredSearchQuery::default()
            }
            .normalized()
            .is_err()
        );
        let query = StructuredSearchQuery {
            entity_id: Some(" entity-1 "),
            ..StructuredSearchQuery::default()
        }
        .normalized();
        assert!(matches!(
            query,
            Ok(StructuredSearchQuery {
                entity_id: Some("entity-1"),
                ..
            })
        ));
    }

    #[test]
    fn token_only_global_query_does_not_embed_filter_syntax() {
        let parsed = parse_global_query("event:trigger modality:vision");
        assert_eq!(parsed.free_text, "");
        assert_eq!(parsed.event_type, Some("trigger"));
        assert_eq!(parsed.modality, Some("vision"));
    }

    #[test]
    fn parse_global_query_removes_filters_after_free_text() {
        let p = parse_global_query(
            "hello world entity_id:e1 event:trigger modality:vision",
        );
        assert_eq!(p.entity_id, Some("e1"));
        assert_eq!(p.event_type, Some("trigger"));
        assert_eq!(p.modality, Some("vision"));
        assert_eq!(p.free_text, "hello world");
    }

    #[tokio::test]
    async fn structured_and_explicit_search_filter_every_result() -> Result<()>
    {
        let context = QueryContext::default();
        let allowed = service(AuthorizationDecision::Allow);
        let hits = allowed
            .structured_search(
                "tenant_a",
                "user_a",
                &context,
                StructuredSearchQuery {
                    text: Some("alpha"),
                    ..StructuredSearchQuery::default()
                },
                10,
            )
            .await?;
        ensure!(hits.len() == 3);
        ensure!(
            allowed
                .search_embeddings_explicit(
                    "tenant_a",
                    "user_a",
                    &context,
                    "alpha",
                    Some("text"),
                    10,
                )
                .await?
                .len() ==
                1
        );
        ensure!(
            allowed
                .search_events_explicit(
                    "tenant_a",
                    "user_a",
                    &context,
                    Some("changed"),
                    None,
                    10,
                )
                .await?
                .len() ==
                1
        );

        let denied = service(AuthorizationDecision::Deny);
        ensure!(
            denied
                .global_search("tenant_a", "user_a", &context, "alpha", 10)
                .await?
                .is_empty()
        );
        let unavailable = service(AuthorizationDecision::Fail);
        ensure!(
            unavailable
                .global_search("tenant_a", "user_a", &context, "alpha", 10)
                .await
                .is_err()
        );
        Ok(())
    }
}
