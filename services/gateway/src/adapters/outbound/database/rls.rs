//! Helpers for enforcing tenant isolation using Postgres RLS with pooled
//! connections.

use anyhow::{Context, Result};
use sqlx::postgres::Postgres;
use sqlx::{Execute, PgPool, QueryBuilder, Row};

pub async fn fetch_rows_with_tenant_guc<'a>(
    pool: &PgPool,
    tenant_id: &'a str,
    select: &mut QueryBuilder<'a, Postgres>,
) -> Result<Vec<serde_json::Value>> {
    let select_sql = select.build().sql().to_string();

    let mut tx = pool
        .begin()
        .await
        .context("Failed to begin tenant-scoped transaction")?;

    sqlx::query("SET LOCAL app.tenant_id = $1")
        .bind(tenant_id)
        .execute(&mut *tx)
        .await
        .context("Failed to SET LOCAL app.tenant_id")?;

    let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
        "SELECT COALESCE(jsonb_agg(to_jsonb(t)), '[]'::jsonb) AS rows FROM (",
    );
    qb.push(select_sql);
    qb.push(") AS t");

    let row = qb
        .build()
        .fetch_one(&mut *tx)
        .await
        .context("Failed to execute tenant-scoped SELECT")?;

    let rows_value: serde_json::Value =
        row.try_get("rows").context("Missing 'rows'")?;

    tx.commit()
        .await
        .context("Failed to commit tenant-scoped transaction")?;

    Ok(rows_value.as_array().cloned().unwrap_or_default())
}
