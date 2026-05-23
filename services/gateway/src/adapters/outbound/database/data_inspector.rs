//! PostgreSQL implementation of DataInspector port.

use std::collections::HashMap;

use anyhow::{Context, Result};
use serde_json::Value;
use sqlx::postgres::Postgres;
use sqlx::{PgPool, QueryBuilder, Row};

use crate::adapters::outbound::database::rls::fetch_rows_with_tenant_guc;
use crate::application::ports::data_inspector::{
    DataInspector, Filter, TableReadSpec,
};
use crate::domain::sink::SinkMetadata;

/// PostgreSQL adapter for introspecting and querying dynamic schemas.
pub struct PgDataIntrospector {
    pool: PgPool,
}

impl PgDataIntrospector {
    /// Creates a new [`PgDataIntrospector`].
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    fn extract_tenant<'a>(filters: &[Filter<'a>]) -> Option<&'a str> {
        filters.iter().find_map(|f| match f {
            Filter::TenantId(t) => Some(*t),
            _ => None,
        })
    }

    fn push_where_clause<'a>(
        qb: &mut QueryBuilder<'a, Postgres>,
        filters: &[Filter<'a>],
    ) {
        if filters.is_empty() {
            return;
        }

        qb.push(" WHERE ");
        let mut sep = qb.separated(" AND ");

        for f in filters {
            match f {
                Filter::TenantId(v) => {
                    sep.push("tenant_id = ");
                    sep.push_bind(*v);
                },
                Filter::EntityId(v) => {
                    sep.push("entity_id = ");
                    sep.push_bind(*v);
                },
                Filter::Modality(v) => {
                    sep.push("modality = ");
                    sep.push_bind(*v);
                },
                Filter::StateType(v) => {
                    sep.push("state_type = ");
                    sep.push_bind(*v);
                },
                Filter::GisZone(v) => {
                    sep.push("metadata->>'zone' = ");
                    sep.push_bind(*v);
                },
            }
        }
    }

    fn allowed_table_idents() -> &'static [&'static str] {
        &["entity_states", "entity_embeddings"]
    }
}

#[async_trait::async_trait]
impl DataInspector for PgDataIntrospector {
    async fn get_available_sinks(&self) -> Result<Vec<SinkMetadata>> {
        // SECURITY: Restrict discovery strictly to allowlisted tables to avoid
        // leaking the full schema.
        let allowed = Self::allowed_table_idents();

        let rows = sqlx::query(
            r#"
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY($1)
            ORDER BY table_name, ordinal_position
            "#,
        )
        .bind(allowed)
        .fetch_all(&self.pool)
        .await
        .context("Failed to fetch allowlisted information schema")?;

        let mut sinks_map: HashMap<String, Vec<String>> = HashMap::new();

        for row in rows {
            let table_name: String = row.try_get("table_name")?;
            let column_name: String = row.try_get("column_name")?;
            sinks_map.entry(table_name).or_default().push(column_name);
        }

        Ok(sinks_map
            .into_iter()
            .map(|(name, columns)| SinkMetadata { name, columns })
            .collect())
    }

    async fn fetch_table_rows<'a>(
        &self,
        spec: TableReadSpec<'a>,
    ) -> Result<Vec<Value>> {
        let tenant_id = Self::extract_tenant(spec.filters)
            .context("TenantId filter is required for RLS isolation")?;

        let limit = spec.limit.clamp(1, 1000);

        let mut select: QueryBuilder<Postgres> =
            QueryBuilder::new("SELECT * FROM \"");
        select.push(spec.table.as_ident());
        select.push("\"");

        Self::push_where_clause(&mut select, spec.filters);

        select.push(" LIMIT ");
        select.push_bind(limit);

        fetch_rows_with_tenant_guc(&self.pool, tenant_id, &mut select).await
    }
}
