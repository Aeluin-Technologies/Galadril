//! Apache AGE adapter for entity relations.

use std::collections::HashSet;

use anyhow::{Context, Result, bail};
use serde_json::Value;
use sqlx::encode::IsNull;
use sqlx::error::BoxDynError;
use sqlx::postgres::{PgArgumentBuffer, PgTypeInfo};
use sqlx::{AssertSqlSafe, Encode, Postgres, Row, Type};

use crate::adapters::outbound::database::connection::{
    Database, tenant_schema_name,
};
use crate::application::ports::relations_store::{
    GraphEdge, GraphNode, GraphSubgraph, RelationsStore,
};

const HARD_LIMIT: usize = 50;
const HARD_K_MAX: u8 = 3;

/// Encodes a raw AGE parameter with its extension-owned PostgreSQL type.
pub(crate) struct AgeParameter(String);

impl AgeParameter {
    /// Serializes a JSON map without assigning PostgreSQL's text or jsonb OID.
    pub(crate) fn from_json(value: &Value) -> Self {
        Self(value.to_string())
    }
}

impl Type<Postgres> for AgeParameter {
    fn type_info() -> PgTypeInfo {
        PgTypeInfo::with_name("agtype")
    }

    fn compatible(ty: &PgTypeInfo) -> bool {
        *ty == PgTypeInfo::with_name("agtype")
    }
}

impl Encode<'_, Postgres> for AgeParameter {
    fn encode_by_ref(
        &self,
        buffer: &mut PgArgumentBuffer,
    ) -> Result<IsNull, BoxDynError> {
        // AGE's binary receive function reserves the first byte for the
        // protocol version and parses the remaining bytes as textual agtype.
        buffer.push(1);
        buffer.extend(self.0.as_bytes());
        Ok(IsNull::No)
    }

    fn size_hint(&self) -> usize {
        self.0.len().saturating_add(1)
    }
}

/// Validates an AGE graph identifier before interpolating it into SQL.
pub(crate) fn validate_graph_name(graph_name: &str) -> Result<&str> {
    let g = graph_name.trim();
    if g.is_empty() {
        bail!("graph_name is empty");
    }
    if !g.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        bail!("graph_name contains invalid characters");
    }
    Ok(g)
}

/// Orders two identifiers for deterministic synthetic edge identity.
fn canonical_pair<'a>(a: &'a str, b: &'a str) -> (&'a str, &'a str) {
    if a <= b { (a, b) } else { (b, a) }
}

pub struct PgAgeRelationsStore {
    database: Database,
}

impl PgAgeRelationsStore {
    /// Creates an AGE relations adapter over tenant-scoped transactions.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Bounds relation result counts to the public traversal contract.
    fn clamp_limit(limit: usize) -> i64 {
        (limit.clamp(1, HARD_LIMIT)) as i64
    }

    /// Bounds traversal depth before generating the static Cypher fragment.
    fn clamp_k(k: u8) -> u8 {
        k.clamp(1, HARD_K_MAX)
    }

    /// Enables AGE operators for the current tenant transaction only.
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

    /// Builds the bounded Cypher traversal query for a validated depth.
    fn cypher_query(k: u8) -> String {
        format!(
            r#"
            MATCH p = (root {{id: $id, tenant_id: $tenant_id}})-[*1..{k}]-(x)
            WHERE all(node IN nodes(p) WHERE node.tenant_id = $tenant_id)
              AND all(edge IN relationships(p) WHERE edge.tenant_id = $tenant_id)
            UNWIND relationships(p) AS r
            RETURN startNode(r) AS from_v, r, endNode(r) AS to_v
            "#
        )
    }

    /// Builds SQL that preserves AGE's required parameter type.
    fn traversal_sql(graph_name: &str, cypher: &str) -> String {
        format!(
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
        )
    }

    /// Extracts a graph vertex while preserving its semantic label and data.
    fn extract_vertex(
        value: Value,
        side: &'static str,
    ) -> Result<(String, String, Value)> {
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

    /// Uses a stored edge identity or derives one deterministically.
    fn extract_edge_id(value: &Value, from_id: &str, to_id: &str) -> String {
        // Prefer AGE edge id if present. If not, build a DIRECTIONLESS id to
        // avoid accidentally encoding direction that may not be
        // meaningful to callers.
        value
            .get("id")
            .and_then(|v| v.as_str())
            .map(str::to_owned)
            .or_else(|| {
                value
                    .get("id")
                    .and_then(|v| v.as_i64())
                    .map(|i| i.to_string())
            })
            .unwrap_or_else(|| {
                let (a, b) = canonical_pair(from_id, to_id);
                let mut s = String::with_capacity(a.len() + 1 + b.len());
                s.push_str(a);
                s.push('-');
                s.push_str(b);
                s
            })
    }

    /// Separates an AGE edge label from its remaining properties.
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
    /// Traverses a bounded tenant AGE neighborhood through RLS.
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

        let mut tx = self.database.tenant(tenant_id).await?;
        Self::set_age_search_path(&mut tx, tenant_id).await?;

        let cypher = Self::cypher_query(k);

        // NOTE: graph name cannot be bound in AGE.
        let query = Self::traversal_sql(graph_name, &cypher);

        let params = serde_json::json!({
            "id": entity_id,
            "tenant_id": tenant_id,
        });

        let rows = sqlx::query(AssertSqlSafe(query))
            // AGE requires a syntactically raw parameter and rejects casts.
            // The wrapper supplies agtype through PostgreSQL's bind protocol.
            .bind(AgeParameter::from_json(&params))
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
        let mut seen_edges: HashSet<String> =
            HashSet::with_capacity(rows.len());

        for row in rows {
            let from_v: Value =
                row.try_get("from_v").context("Missing from_v")?;
            let r: Value = row.try_get("r").context("Missing r")?;
            let to_v: Value = row.try_get("to_v").context("Missing to_v")?;

            let (from_id, from_label, from_props) =
                Self::extract_vertex(from_v, "from_v")?;
            if seen_nodes.insert(from_id.clone()) {
                nodes.push(GraphNode {
                    id: from_id.clone(),
                    label: from_label,
                    properties: from_props,
                });
            }

            let (to_id, to_label, to_props) =
                Self::extract_vertex(to_v, "to_v")?;
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
    use anyhow::{Context, Result};
    use sqlx::postgres::PgPoolOptions;
    use testcontainers_modules::testcontainers::core::{
        IntoContainerPort, WaitFor,
    };
    use testcontainers_modules::testcontainers::runners::AsyncRunner;
    use testcontainers_modules::testcontainers::{GenericImage, ImageExt};

    use super::*;

    #[test]
    fn validate_graph_name_is_strict() -> anyhow::Result<()> {
        assert_eq!(validate_graph_name("galadril_graph")?, "galadril_graph");
        assert!(validate_graph_name("").is_err());
        assert!(validate_graph_name(" ").is_err());
        assert!(validate_graph_name("a-b").is_err());
        assert!(validate_graph_name("a;drop").is_err());
        assert!(validate_graph_name("a/b").is_err());
        Ok(())
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

    #[test]
    fn traversal_sql_keeps_age_parameter_unmodified() {
        let query =
            PgAgeRelationsStore::traversal_sql("galadril_dev", "RETURN $id");

        assert!(query.contains("$$, $1)"));
        assert!(!query.contains("$1::"));
    }

    #[test]
    fn age_parameter_declares_the_extension_type() {
        assert_eq!(
            <AgeParameter as Type<Postgres>>::type_info(),
            PgTypeInfo::with_name("agtype")
        );
    }

    #[tokio::test]
    async fn age_parameter_uses_the_postgres_bind_protocol() -> Result<()> {
        let container = GenericImage::new(
            "ghcr.io/aeluin-technologies/galadril-database",
            "latest",
        )
        .with_exposed_port(5432.tcp())
        .with_wait_for(WaitFor::message_on_stderr(
            "database system is ready to accept connections",
        ))
        .with_wait_for(WaitFor::message_on_stderr(
            "database system is ready to accept connections",
        ))
        .with_cmd([
            "postgres",
            "-c",
            "listen_addresses=*",
            "-c",
            "shared_preload_libraries=timescaledb,age,pg_cron,pg_stat_statements,pg_wait_sampling",
        ])
        .with_env_var("POSTGRES_PASSWORD", "postgres")
        .start()
        .await
        .context("AGE test container failed")?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect(&format!(
                "postgres://postgres:postgres@{host}:{port}/postgres"
            ))
            .await?;
        let mut connection = pool.acquire().await?;
        sqlx::query("SET search_path = ag_catalog, public")
            .execute(&mut *connection)
            .await?;
        sqlx::query("SELECT create_graph('age_parameter_test')")
            .execute(&mut *connection)
            .await?;

        let value: Value = sqlx::query_scalar(
            r#"
            SELECT agtype_to_jsonb(value)
            FROM cypher('age_parameter_test', $$
              RETURN $value
            $$, $1) AS (value agtype)
            "#,
        )
        .bind(AgeParameter::from_json(
            &serde_json::json!({"value": "bound"}),
        ))
        .fetch_one(&mut *connection)
        .await?;

        assert_eq!(value, serde_json::json!("bound"));
        Ok(())
    }
}
