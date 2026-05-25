//! Use cases for exploring and querying data with fine-grained access control.

use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Result, bail};
use serde_json::Value;
use tokio::sync::RwLock;

use crate::application::ports::data_inspector::{
    AllowedTable, DataInspector, Filter, TableReadSpec,
};
use crate::application::usecases::authorization::{
    AuthService, Permission, QueryContext,
};
use crate::domain::sink::SinkMetadata;

const ALLOWED_TABLES: &[AllowedTable] =
    &[AllowedTable::EntityStates, AllowedTable::EntityEmbeddings];

/// Internal cache structure for available tables.
struct TableCache {
    tables: Arc<Vec<SinkMetadata>>,
    expires_at: Instant,
}

/// Service responsible for fetching data securely with FGAC.
pub struct DataExplorerService {
    data_introspector: Arc<dyn DataInspector>,
    auth_service: Arc<AuthService>,
    cache: RwLock<Option<TableCache>>,
    cache_ttl: Duration,
}

impl DataExplorerService {
    /// Creates a new [`DataExplorerService`].
    pub fn new(
        data_introspector: Arc<dyn DataInspector>,
        auth_service: Arc<AuthService>,
        cache_ttl: Duration,
    ) -> Self {
        Self {
            data_introspector,
            auth_service,
            cache: RwLock::new(None),
            cache_ttl,
        }
    }

    /// Invalidates the cached table list.
    pub async fn invalidate_cache(&self) {
        let mut cache_guard = self.cache.write().await;
        *cache_guard = None;
    }

    async fn get_allowed_tables(&self) -> Result<Arc<Vec<SinkMetadata>>> {
        {
            let cache_guard = self.cache.read().await;
            if let Some(cache) = &*cache_guard &&
                cache.expires_at > Instant::now()
            {
                return Ok(Arc::clone(&cache.tables));
            }
        }

        let all_tables = self.data_introspector.get_available_sinks().await?;

        let filtered_tables: Vec<SinkMetadata> = all_tables
            .into_iter()
            .filter(|t| {
                ALLOWED_TABLES.iter().any(|at| at.as_ident() == t.name)
            })
            .collect();

        let arc_tables = Arc::new(filtered_tables);

        let mut cache_guard = self.cache.write().await;
        *cache_guard = Some(TableCache {
            tables: Arc::clone(&arc_tables),
            expires_at: Instant::now() + self.cache_ttl,
        });

        Ok(arc_tables)
    }

    /// Returns the tables the user is authorized to discover.
    pub async fn get_authorized_tables(
        &self,
        _tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<SinkMetadata>> {
        let tables = self.get_allowed_tables().await?;
        let table_ids: Vec<String> =
            tables.iter().map(|s| s.name.clone()).collect();

        let allowed_ids = self
            .auth_service
            .filter_authorized_resources(
                user_id,
                Permission::Read,
                "table",
                &table_ids,
            )
            .await?;

        Ok(tables
            .iter()
            .filter(|s| allowed_ids.contains(&s.name))
            .cloned()
            .collect())
    }

    /// Queries a specific table applying Cedar FGAC (now via SpiceDB) and
    /// DB-level RLS.
    pub async fn query_table(
        &self,
        tenant_id: &str,
        user_id: &str,
        table_name: &str,
        limit: usize,
        query_context: Option<QueryContext>,
    ) -> Result<Vec<Value>> {
        let table = parse_allowed_table(table_name).ok_or_else(|| {
            anyhow::anyhow!(
                "Table '{table_name}' is not allowed or does not exist"
            )
        })?;

        let safe_limit = (limit as i64).clamp(1, 1000);

        let is_allowed = self
            .auth_service
            .is_authorized(
                user_id,
                Permission::Read,
                "table",
                table_name,
                query_context.as_ref(),
            )
            .await?;

        if !is_allowed {
            bail!(
                "User '{user_id}' is not authorized to read from table '{table_name}'"
            );
        }

        let mut filters: Vec<Filter<'_>> = Vec::with_capacity(5);
        filters.push(Filter::TenantId(tenant_id));

        if let Some(ctx) = &query_context {
            if let Some(v) = ctx.entity_id.as_deref() {
                filters.push(Filter::EntityId(v));
            }
            if let Some(v) = ctx.modality.as_deref() {
                filters.push(Filter::Modality(v));
            }
            if let Some(v) = ctx.state_type.as_deref() {
                filters.push(Filter::StateType(v));
            }
            if let Some(v) = ctx.gis_zone.as_deref() {
                filters.push(Filter::GisZone(v));
            }
        }

        let spec = TableReadSpec {
            table,
            limit: safe_limit,
            filters: &filters,
        };

        self.data_introspector.fetch_table_rows(spec).await
    }
}

fn parse_allowed_table(name: &str) -> Option<AllowedTable> {
    match name {
        "entity_states" => Some(AllowedTable::EntityStates),
        "entity_embeddings" => Some(AllowedTable::EntityEmbeddings),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_allowed_table_is_strict() {
        assert_eq!(
            parse_allowed_table("entity_states"),
            Some(AllowedTable::EntityStates)
        );
        assert_eq!(
            parse_allowed_table("entity_embeddings"),
            Some(AllowedTable::EntityEmbeddings)
        );
        assert_eq!(parse_allowed_table("public.entity_states"), None);
        assert_eq!(parse_allowed_table("entity_states;DROP TABLE x"), None);
    }
}
