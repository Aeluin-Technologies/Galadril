//! Entity exploration use cases (search + relations) with permission filtering
//! via Loth.

use std::collections::HashSet;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde_json::Value;

use crate::application::ports::entity_state_store::EntityStateStore;
use crate::application::ports::relations_store::{
    GraphEdge, GraphNode, GraphSubgraph, RelationsStore,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
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
    auth: Arc<dyn Authorization>,
    /// AGE graph name within each tenant schema.
    graph_name: String,
}

impl ExploreService {
    /// Creates the exploration service over search, graph, and authorization.
    pub fn new(
        states: Arc<dyn EntityStateStore>,
        relations: Arc<dyn RelationsStore>,
        auth: Arc<dyn Authorization>,
        graph_name: impl Into<String>,
    ) -> Self {
        Self {
            states,
            relations,
            auth,
            graph_name: graph_name.into(),
        }
    }

    /// Searches entity states and returns only individually visible results.
    pub async fn search_entities_by_name(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
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
                ..policy_context.clone()
            };

            let ok = self
                .auth
                .is_authorized(
                    user_id,
                    tenant_id,
                    Permission::View,
                    "entity_state",
                    &row.entity_id,
                    Some(&ctx),
                )
                .await
                .context("Failed to authorize search hit")?;

            let source_visible = if let Some(event_id) = row
                .metadata
                .get("event_id")
                .and_then(Value::as_str)
                .filter(|event_id| !event_id.is_empty())
            {
                self.auth
                    .is_authorized(
                        user_id,
                        tenant_id,
                        Permission::View,
                        "event",
                        event_id,
                        Some(&ctx),
                    )
                    .await
                    .context("Failed to authorize search evidence")?
            } else {
                false
            };
            if ok && source_visible {
                out.push(SearchHit {
                    entity_id: row.entity_id,
                    metadata: row.metadata,
                });
            }
        }

        Ok(out)
    }

    /// Hydrates a graph neighborhood and filters every node and edge by view.
    pub async fn entity_relations_filtered(
        &self,
        tenant_id: &str,
        user_id: &str,
        policy_context: &QueryContext,
        entity_id: &str,
        depth: u8,
        limit: usize,
    ) -> Result<GraphSubgraph> {
        let lim = limit.clamp(1, HARD_LIMIT);

        let administrator = self
            .auth
            .is_authorized(
                user_id,
                tenant_id,
                Permission::Manage,
                "tenant",
                tenant_id,
                Some(policy_context),
            )
            .await
            .context("Failed to authorize graph property access")?;
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

        let mut allowed_nodes: HashSet<String> =
            HashSet::with_capacity(raw.nodes.len());
        let mut filtered_nodes: Vec<GraphNode> =
            Vec::with_capacity(raw.nodes.len());

        for n in raw.nodes {
            let (resource_type, resource_id) = map_graph_node_to_resource(&n);

            let ctx = QueryContext {
                entity_id: Some(n.id.clone()),
                modality: None,
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
                    resource_type,
                    resource_id,
                    Some(&ctx),
                )
                .await
                .context("Failed to authorize relation node")?;

            if ok {
                allowed_nodes.insert(n.id.clone());
                filtered_nodes.push(GraphNode {
                    id: n.id,
                    label: n.label,
                    properties: if administrator {
                        n.properties
                    } else {
                        serde_json::json!({})
                    },
                });
            }
        }

        let mut filtered_edges: Vec<GraphEdge> =
            Vec::with_capacity(raw.edges.len());
        for e in raw.edges {
            if allowed_nodes.contains(&e.from_id) &&
                allowed_nodes.contains(&e.to_id) &&
                (e.from_id.starts_with("evt_") ||
                    e.to_id.starts_with("evt_"))
            {
                filtered_edges.push(GraphEdge {
                    from_id: e.from_id,
                    to_id: e.to_id,
                    label: e.label,
                    properties: if administrator {
                        e.properties
                    } else {
                        serde_json::json!({})
                    },
                });
            }
        }

        Ok(GraphSubgraph {
            nodes: filtered_nodes,
            edges: filtered_edges,
        })
    }
}

/// Maps the current AGE node contract to its SpiceDB resource namespace.
fn map_graph_node_to_resource(n: &GraphNode) -> (&'static str, &str) {
    if n.id.starts_with("evt_") {
        ("event", n.id.as_str())
    } else {
        ("entity_state", n.id.as_str())
    }
}

#[cfg(test)]
mod tests {
    use anyhow::Result;

    use super::*;

    struct MixedGraph;

    #[async_trait::async_trait]
    impl RelationsStore for MixedGraph {
        async fn k_hop_neighbors(
            &self,
            _tenant_id: &str,
            _graph_name: &str,
            _entity_id: &str,
            _k: u8,
            _limit: usize,
        ) -> Result<GraphSubgraph> {
            Ok(GraphSubgraph {
                nodes: vec![
                    GraphNode {
                        id: "entity-1".into(),
                        label: "Person".into(),
                        properties: serde_json::json!({"label":"private"}),
                    },
                    GraphNode {
                        id: "evt_visible".into(),
                        label: "Observation".into(),
                        properties: serde_json::json!({"source":"visible"}),
                    },
                    GraphNode {
                        id: "evt_hidden".into(),
                        label: "Observation".into(),
                        properties: serde_json::json!({"source":"hidden"}),
                    },
                ],
                edges: vec![
                    GraphEdge {
                        from_id: "entity-1".into(),
                        to_id: "evt_visible".into(),
                        label: "OBSERVED".into(),
                        properties: serde_json::json!({"confidence":0.9}),
                    },
                    GraphEdge {
                        from_id: "entity-1".into(),
                        to_id: "evt_hidden".into(),
                        label: "OBSERVED".into(),
                        properties: serde_json::json!({"confidence":0.8}),
                    },
                ],
            })
        }
    }

    struct VisibleEventAuthorization;

    #[async_trait::async_trait]
    impl Authorization for VisibleEventAuthorization {
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

        async fn is_authorized(
            &self,
            user_id: &str,
            _: &str,
            _: Permission,
            resource_type: &str,
            resource_id: &str,
            _: Option<&QueryContext>,
        ) -> Result<bool> {
            Ok(user_id == "admin" ||
                resource_type == "entity_state" ||
                (resource_type == "event" && resource_id == "evt_visible"))
        }

        async fn invalidate_tenant_cache(&self, _: &str) {}
    }

    #[test]
    fn map_graph_node_defaults_to_entity_state() {
        let n = GraphNode {
            id: "e1".to_string(),
            label: "Whatever".to_string(),
            properties: serde_json::json!({}),
        };

        let (t, id) = map_graph_node_to_resource(&n);
        assert_eq!(t, "entity_state");
        assert_eq!(id, "e1");
    }

    #[tokio::test]
    async fn graph_projection_excludes_hidden_event_and_unattributed_properties()
    -> Result<()> {
        struct EmptyStates;
        #[async_trait::async_trait]
        impl EntityStateStore for EmptyStates {
            async fn search_by_name(&self, _: &str, _: &str, _: usize) -> Result<Vec<crate::application::ports::entity_state_store::EntityStateRow>>{
                Ok(Vec::new())
            }

            async fn latest_states_for_entity(&self, _: &str, _: &str, _: usize) -> Result<Vec<crate::application::ports::entity_state_store::EntityStateRow>>{
                Ok(Vec::new())
            }
        }
        let service = ExploreService::new(
            Arc::new(EmptyStates),
            Arc::new(MixedGraph),
            Arc::new(VisibleEventAuthorization),
            "graph",
        );
        let graph = service
            .entity_relations_filtered(
                "tenant-a",
                "reader",
                &QueryContext::default(),
                "entity-1",
                1,
                10,
            )
            .await?;
        assert_eq!(graph.nodes.len(), 2);
        assert!(graph.nodes.iter().all(|node| node.id != "evt_hidden" &&
            node.properties == serde_json::json!({})));
        assert_eq!(graph.edges.len(), 1);
        assert!(graph.nodes.iter().any(|node| node.label == "Person"));
        assert!(graph.nodes.iter().any(|node| node.label == "Observation"));
        assert_eq!(
            graph.edges.first().map(|edge| edge.to_id.as_str()),
            Some("evt_visible")
        );
        assert_eq!(
            graph.edges.first().map(|edge| &edge.properties),
            Some(&serde_json::json!({}))
        );
        let admin_graph = service
            .entity_relations_filtered(
                "tenant-a",
                "admin",
                &QueryContext::default(),
                "entity-1",
                1,
                10,
            )
            .await?;
        assert_eq!(admin_graph.nodes.len(), 3);
        assert_eq!(admin_graph.edges.len(), 2);
        assert_eq!(
            admin_graph.nodes.first().map(|node| &node.properties),
            Some(&serde_json::json!({"label":"private"}))
        );
        assert_eq!(
            admin_graph.edges.first().map(|edge| &edge.properties),
            Some(&serde_json::json!({"confidence":0.9}))
        );
        Ok(())
    }
}
