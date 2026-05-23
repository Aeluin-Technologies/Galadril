//! Outbound port for retrieving graph relations for an entity.

use anyhow::Result;
use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct GraphNode {
    pub id: String,
    pub label: String,
    pub properties: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct GraphEdge {
    pub from_id: String,
    pub to_id: String,
    pub label: String,
    pub properties: Value,
}

#[derive(Debug, Clone, PartialEq)]
pub struct GraphSubgraph {
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<GraphEdge>,
}

#[async_trait::async_trait]
pub trait RelationsStore: Send + Sync {
    /// Retrieves a bounded k-hop neighborhood around `entity_id`.
    async fn k_hop_neighbors(
        &self,
        tenant_id: &str,
        graph_name: &str,
        entity_id: &str,
        k: u8,
        limit: usize,
    ) -> Result<GraphSubgraph>;
}
