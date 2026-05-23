//! Apache AGE adapter for entity relations.

use std::collections::HashSet;

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sqlx::{PgPool, Row};

use crate::adapters::outbound::database::tenant_schema::{
    begin_tenant_tx, tenant_schema_name,
};
use crate::application::ports::relations_store::{
    GraphEdge, GraphNode, GraphSubgraph, RelationsStore,
};

const HARD_LIMIT: usize = 50;
const HARD_K_MAX: u8 = 3;

fn validate_graph_name(graph_name: &str) -> Result<&str> {
    let g = graph_name.trim();
    if g.is_empty() {
        bail!("graph_name is empty");
    }
    if !g.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        bail!("graph_name contains invalid characters");
    }
    Ok(g)
}

fn canonical_pair<'a>(a: &'a str, b: &'a str) -> (&'a str, &'a str) {
    if a <= b { (a, b) } else { (b, a) }
}

pub struct PgAgeRelationsStore {
    pool: PgPool,
}

impl PgAgeRelationsStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    fn clamp_limit(limit: usize) -> i64 {
        (limit.clamp(1, HARD_LIMIT)) as i64
    }

    fn clamp_k(k: u8) -> u8 {
        k.clamp(1, HARD_K_MAX)
    }

    async fn set_age_search_path(
        tx: &mut sqlx::Transaction<'static, sqlx::Postgres>,
        tenant_id: &str,
    ) -> Result<()> {
        let schema = tenant_schema_name(tenant_id)?;
        sqlx::query("SELECT set_config('search_path', $1, true)")
            .bind(format!("ag_catalog, {schema}, public"))
            .execute(&mut **tx)
            .await
            .context("Failed to set search_path for AGE")?;
        Ok(())
    }

    fn cypher_query(k: u8) -> String {
        format!(
            r#"
            MATCH p = (root {{id: $id}})-[*1..{k}]-(x)
            UNWIND relationships(p) AS r
            RETURN startNode(r) AS from_v, r, endNode(r) AS to_v
            "#
        )
    }

    fn extract_vertex(value: Value, side: &'static str) -> Result<(String, String, Value)> {
        let props = value
            .get("properties")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("Missing {side}.properties"))?;

        let id = props
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| anyhow::anyhow!("Missing {side}.properties.id"))?
            .to_string();

        let label = value
            .get("label")
            .and_then(|v| v.as_str())
            .unwrap_or("Unknown")
            .to_string();

        Ok((id, label, props))
    }

    fn extract_edge_id(value: &Value, from_id: &str, to_id: &str) -> String {
        // Prefer AGE edge id if present. If not, build a DIRECTIONLESS id to avoid
        // accidentally encoding direction that may not be meaningful to callers.
        value
            .get("id")
            .and_then(|v| v.as_str())
            .map(str::to_owned)
            .or_else(|| value.get("id").and_then(|v| v.as_i64()).map(|i| i.to_string()))
            .unwrap_or_else(|| {
                let (a, b) = canonical_pair(from_id, to_id);
                let mut s = String::with_capacity(a.len() + 1 + b.len());
                s.push_str(a);
                s.push('-');
                s.push_str(b);
                s
            })
    }

    fn extract_edge_label_props(value: Value) -> (String, Value) {
        let label = value
            .get("label")
            .and_then(|v| v.as_str())
            .unwrap_or("RELATED_TO")
            .to_string();

        let props = value
            .get("properties")
            .cloned()
            .unwrap_or_else(|| Value::Object(Default::default()));

        (label, props)
    }
}

#[async_trait::async_trait]
impl RelationsStore for PgAgeRelationsStore {
    async fn k_hop_neighbors(
        &self,
        tenant_id: &str,
        graph_name: &str,
        entity_id: &str,
        k: u8,
        limit: usize,
    ) -> Result<GraphSubgraph> {
        let graph_name = validate_graph_name(graph_name)?;
        let lim = Self::clamp_limit(limit);
        let k = Self::clamp_k(k);

        let mut tx = begin_tenant_tx(&self.pool, tenant_id).await?;
        Self::set_age_search_path(&mut tx, tenant_id).await?;

        let cypher = Self::cypher_query(k);

        // NOTE: graph name cannot be bound in AGE.
        let query = format!(
            r#"
            SELECT
              agtype_to_jsonb(from_v) AS from_v,
              agtype_to_jsonb(r) AS r,
              agtype_to_jsonb(to_v) AS to_v
            FROM cypher('{graph_name}', $$
              {cypher}
            $$, $1) AS (from_v agtype, r agtype, to_v agtype)
            LIMIT $2
            "#
        );

        let params = serde_json::json!({ "id": entity_id });

        let rows = sqlx::query(&query)
            .bind(params)
            .bind(lim)
            .fetch_all(&mut *tx)
            .await
            .context("Failed to query AGE neighbors")?;

        tx.commit()
            .await
            .context("Failed to commit AGE neighbors tx")?;

        let mut nodes = Vec::with_capacity(rows.len().saturating_mul(2));
        let mut edges = Vec::with_capacity(rows.len());

        let mut seen_nodes: HashSet<String> =
            HashSet::with_capacity(rows.len().saturating_mul(2));
        let mut seen_edges: HashSet<String> = HashSet::with_capacity(rows.len());

        for row in rows {
            let from_v: Value = row.try_get("from_v").context("Missing from_v")?;
            let r: Value = row.try_get("r").context("Missing r")?;
            let to_v: Value = row.try_get("to_v").context("Missing to_v")?;

            let (from_id, from_label, from_props) = Self::extract_vertex(from_v, "from_v")?;
            if seen_nodes.insert(from_id.clone()) {
                nodes.push(GraphNode {
                    id: from_id.clone(),
                    label: from_label,
                    properties: from_props,
                });
            }

            let (to_id, to_label, to_props) = Self::extract_vertex(to_v, "to_v")?;
            if seen_nodes.insert(to_id.clone()) {
                nodes.push(GraphNode {
                    id: to_id.clone(),
                    label: to_label,
                    properties: to_props,
                });
            }

            let edge_id = Self::extract_edge_id(&r, &from_id, &to_id);
            if seen_edges.insert(edge_id) {
                let (label, props) = Self::extract_edge_label_props(r);
                edges.push(GraphEdge {
                    from_id,
                    to_id,
                    label,
                    properties: props,
                });
            }
        }

        Ok(GraphSubgraph { nodes, edges })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_graph_name_is_strict() {
        assert_eq!(validate_graph_name("galadril_graph").unwrap(), "galadril_graph");
        assert!(validate_graph_name("").is_err());
        assert!(validate_graph_name(" ").is_err());
        assert!(validate_graph_name("a-b").is_err());
        assert!(validate_graph_name("a;drop").is_err());
        assert!(validate_graph_name("a/b").is_err());
    }

    #[test]
    fn canonical_pair_is_stable() {
        assert_eq!(canonical_pair("b", "a"), ("a", "b"));
        assert_eq!(canonical_pair("a", "b"), ("a", "b"));
    }

    #[test]
    fn clamp_limit_and_k_are_bounded() {
        assert_eq!(PgAgeRelationsStore::clamp_limit(0), 1);
        assert_eq!(PgAgeRelationsStore::clamp_limit(999), 50);
        assert_eq!(PgAgeRelationsStore::clamp_k(0), 1);
        assert_eq!(PgAgeRelationsStore::clamp_k(9), 3);
    }
}
