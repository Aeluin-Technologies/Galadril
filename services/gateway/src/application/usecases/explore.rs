//! Entity exploration use cases (search + relations) with Cedar filtering.

use std::collections::HashSet;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde_json::Value;

use crate::application::ports::entity_state_store::EntityStateStore;
use crate::application::ports::relations_store::{
    GraphEdge, GraphNode, GraphSubgraph, RelationsStore,
};
use crate::application::usecases::authorization::{
    Action, AuthService, QueryContext,
};

const HARD_LIMIT: usize = 50;

#[derive(Debug, Clone)]
pub struct SearchHit {
    pub entity_id: String,
    pub metadata: Value,
}

pub struct ExploreService {
    states: Arc<dyn EntityStateStore>,
    relations: Arc<dyn RelationsStore>,
    auth: Arc<AuthService>,
    /// AGE graph name within each tenant schema.
    graph_name: String,
}

impl ExploreService {
    pub fn new(
        states: Arc<dyn EntityStateStore>,
        relations: Arc<dyn RelationsStore>,
        auth: Arc<AuthService>,
        graph_name: impl Into<String>,
    ) -> Self {
        Self {
            states,
            relations,
            auth,
            graph_name: graph_name.into(),
        }
    }

    pub async fn search_entities_by_name(
        &self,
        tenant_id: &str,
        user_id: &str,
        query: &str,
        limit: usize,
    ) -> Result<Vec<SearchHit>> {
        let lim = limit.clamp(1, HARD_LIMIT);
        let candidates = self
            .states
            .search_by_name(tenant_id, query, lim)
            .await
            .context("Failed to search candidates")?;

        let mut out = Vec::with_capacity(candidates.len());
        for row in candidates {
            let ctx = QueryContext {
                entity_id: Some(row.entity_id.clone()),
                modality: None,
                state_type: row.state_type.clone(),
                gis_zone: None,
            };

            let ok = self
                .auth
                .is_authorized(
                    tenant_id,
                    user_id,
                    Action::ReadEntityState,
                    "entity_states",
                    Some(&ctx),
                )
                .await
                .context("Failed to Cedar-authorize search hit")?;

            if ok {
                out.push(SearchHit {
                    entity_id: row.entity_id,
                    metadata: row.metadata,
                });
            }
        }

        Ok(out)
    }

    pub async fn entity_relations_filtered(
        &self,
        tenant_id: &str,
        user_id: &str,
        entity_id: &str,
        depth: u8,
        limit: usize,
    ) -> Result<GraphSubgraph> {
        let lim = limit.clamp(1, HARD_LIMIT);

        let raw = self
            .relations
            .k_hop_neighbors(
                tenant_id,
                &self.graph_name,
                entity_id,
                depth,
                lim,
            )
            .await
            .context("Failed to fetch relations from AGE")?;

        // Filter nodes by Cedar (ReadEntityRelations). Edges are only kept if
        // both endpoints survive.
        let mut allowed_nodes: HashSet<String> =
            HashSet::with_capacity(raw.nodes.len());
        let mut filtered_nodes: Vec<GraphNode> =
            Vec::with_capacity(raw.nodes.len());

        for n in raw.nodes {
            let ctx = QueryContext {
                entity_id: Some(n.id.clone()),
                modality: None,
                state_type: None,
                gis_zone: None,
            };

            let ok = self
                .auth
                .is_authorized(
                    tenant_id,
                    user_id,
                    Action::ReadEntityRelations,
                    "entity_graph",
                    Some(&ctx),
                )
                .await
                .context("Failed to Cedar-authorize relation node")?;

            if ok {
                allowed_nodes.insert(n.id.clone());
                filtered_nodes.push(n);
            }
        }

        let mut filtered_edges: Vec<GraphEdge> =
            Vec::with_capacity(raw.edges.len());
        for e in raw.edges {
            if allowed_nodes.contains(&e.from_id) &&
                allowed_nodes.contains(&e.to_id)
            {
                filtered_edges.push(e);
            }
        }

        Ok(GraphSubgraph {
            nodes: filtered_nodes,
            edges: filtered_edges,
        })
    }
}
