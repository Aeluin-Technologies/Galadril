//! Outbound port for database inspection and safe, parameterized reads.

use anyhow::Result;
use serde_json::Value;

use crate::domain::sink::SinkMetadata;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AllowedTable {
    EntityStates,
    EntityEmbeddings,
}

impl AllowedTable {
    pub fn as_ident(self) -> &'static str {
        match self {
            AllowedTable::EntityStates => "entity_states",
            AllowedTable::EntityEmbeddings => "entity_embeddings",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Filter<'a> {
    TenantId(&'a str),
    EntityId(&'a str),
    Modality(&'a str),
    StateType(&'a str),
    GisZone(&'a str),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableReadSpec<'a> {
    pub table: AllowedTable,
    pub limit: i64,
    pub filters: &'a [Filter<'a>],
}

#[async_trait::async_trait]
pub trait DataInspector: Send + Sync {
    /// Retrieves all available sinks (tables) and their columns.
    async fn get_available_sinks(&self) -> Result<Vec<SinkMetadata>>;

    /// Executes a safe, parameterized query for an allowlisted table.
    async fn fetch_table_rows<'a>(
        &self,
        spec: TableReadSpec<'a>,
    ) -> Result<Vec<Value>>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowed_table_ident_is_stable() {
        assert_eq!(AllowedTable::EntityStates.as_ident(), "entity_states");
    }
}
