//! Helpers for enforcing tenant isolation using Postgres RLS with pooled
//! connections.
//!
//! This module is defense-in-depth only. Application authorization must be
//! enforced via SpiceDB/Cedar (Loth) and should fail-closed. RLS prevents
//! cross-tenant leakage if an application bug bypasses checks.

use anyhow::{Context, Result};
use sqlx::{PgPool, Postgres, Transaction};

/// Begins a transaction with a tenant-scoped GUC (`SET LOCAL app.tenant_id`).
///
/// Callers should keep the transaction short to reduce tail latency.
pub async fn begin_rls_tx<'p>(
    pool: &'p PgPool,
    tenant_id: &str,
) -> Result<Transaction<'p, Postgres>> {
    let mut tx = pool
        .begin()
        .await
        .context("Failed to begin tenant-scoped transaction")?;

    sqlx::query("SET LOCAL app.tenant_id = $1")
        .bind(tenant_id)
        .execute(&mut *tx)
        .await
        .context("Failed to SET LOCAL app.tenant_id")?;

    Ok(tx)
}
