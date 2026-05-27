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
    AuthService, Permission, QueryContext,
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

#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct ParsedQuery<'a> {
    free_text: &'a str,
    entity_id: Option<&'a str>,
    event_type: Option<&'a str>,
    modality: Option<&'a str>,
}

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

    let mut free_start = 0usize;
    let bytes = out.free_text.as_bytes();

    // Scan tokens by whitespace; keep the first occurrence of known keys.
    for (i, part) in out.free_text.split_whitespace().enumerate() {
        let _ = i;
        if let Some((k, v)) = part.split_once(':') {
            match k {
                "entity_id" if out.entity_id.is_none() => {
                    out.entity_id = Some(v)
                },
                "event" if out.event_type.is_none() => {
                    out.event_type = Some(v)
                },
                "modality" if out.modality.is_none() => out.modality = Some(v),
                _ => {},
            }
        } else if free_start == 0 {
            // Compute offset by finding substring position (safe but O(n)
            // worst case).
            if let Some(pos) = out.free_text.find(part) {
                free_start = pos;
            }
        }
    }

    if free_start != 0 && free_start < bytes.len() {
        out.free_text = out.free_text[free_start..].trim();
    } else {
        // If everything looked like tokens, keep full string for embedding.
        out.free_text = q.trim();
    }

    out
}

pub struct SearchService {
    states: Arc<dyn EntityStateStore>,
    store: Arc<dyn SearchStore>,
    embed: Arc<dyn EmbeddingGenerator>,
    auth: Arc<AuthService>,
}

impl SearchService {
    pub fn new(
        states: Arc<dyn EntityStateStore>,
        store: Arc<dyn SearchStore>,
        embed: Arc<dyn EmbeddingGenerator>,
        auth: Arc<AuthService>,
    ) -> Self {
        Self {
            states,
            store,
            embed,
            auth,
        }
    }

    pub async fn global_search(
        &self,
        tenant_id: &str,
        user_id: &str,
        query: &str,
        limit: usize,
    ) -> Result<Vec<GlobalSearchHit>> {
        let lim = limit.clamp(1, HARD_LIMIT);
        let parsed = parse_global_query(query);

        if parsed.free_text.is_empty() {
            bail!("global_search query is empty");
        }

        let state_rows = self
            .states
            .search_by_name(tenant_id, parsed.free_text, lim)
            .await
            .context("Failed to search entity_states")?;

        let mut out: Vec<GlobalSearchHit> =
            Vec::with_capacity(lim.saturating_mul(2));

        for row in state_rows {
            let ctx = QueryContext {
                entity_id: Some(row.entity_id.clone()),
                modality: None,
                state_type: row.state_type.clone(),
                gis_zone: None,
            };

            let ok = self
                .auth
                .is_authorized(
                    user_id,
                    Permission::Read,
                    "entity_state",
                    &row.entity_id,
                    Some(&ctx),
                )
                .await
                .context("Failed to authorize entity_state hit")?;

            if ok {
                out.push(GlobalSearchHit::EntityState {
                    entity_id: row.entity_id,
                    state: row.metadata,
                });
            }
        }

        let do_events =
            parsed.event_type.is_some() || parsed.free_text.len() >= 3;
        if do_events && out.len() < lim {
            let events = self
                .store
                .search_events(
                    tenant_id,
                    parsed.event_type,
                    Some(parsed.free_text),
                    lim.saturating_sub(out.len()),
                )
                .await
                .context("Failed to search eskg_events")?;

            for e in events {
                out.push(GlobalSearchHit::Event {
                    event_id: e.event_id,
                    event_type: e.event_type,
                    event_time_ms: e.event_time_ms,
                    properties: e.properties,
                });
            }
        }

        if out.len() < lim {
            let emb = self
                .embed
                .embed_text(parsed.free_text)
                .await
                .context("Failed to embed global_search text")?;

            let rows = self
                .store
                .search_embeddings_top_k(
                    tenant_id,
                    parsed.modality,
                    &emb,
                    lim.saturating_sub(out.len()),
                )
                .await
                .context("Failed to search entity_embeddings")?;

            self.push_authorized_embeddings(user_id, rows, &mut out, lim)
                .await?;
        }

        out.truncate(lim);
        Ok(out)
    }

    async fn push_authorized_embeddings(
        &self,
        user_id: &str,
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
            };

            // Authorize by entity_id using the entity_state object type.
            let ok = self
                .auth
                .is_authorized(
                    user_id,
                    Permission::Read,
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

    pub async fn search_embeddings_explicit(
        &self,
        tenant_id: &str,
        user_id: &str,
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
            };
            let ok = self
                .auth
                .is_authorized(
                    user_id,
                    Permission::Read,
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

    pub async fn search_events_explicit(
        &self,
        tenant_id: &str,
        _user_id: &str,
        event_type: Option<&str>,
        text: Option<&str>,
        limit: usize,
    ) -> Result<Vec<EventRow>> {
        self.store
            .search_events(tenant_id, event_type, text, limit)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
