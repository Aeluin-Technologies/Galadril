//! Tenant schema scoping helpers.

use anyhow::{Context, Result, bail};
use sqlx::{PgConnection, PgPool, Postgres, Transaction};

/// Maximum size to prevent abuse.
const MAX_TENANT_ID_LEN: usize = 64;

/// Returns a safe schema name like `tenant_<id>`.
pub fn tenant_schema_name(tenant_id: &str) -> Result<String> {
    let tid = tenant_id.trim();
    if tid.is_empty() {
        bail!("tenant_id is empty");
    }
    if tid.len() > MAX_TENANT_ID_LEN {
        bail!("tenant_id is too long");
    }

    if !tid
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        bail!("tenant_id contains invalid characters");
    }

    // Keep schema naming stable and predictable.
    Ok(format!("tenant_{tid}"))
}

/// Begins a transaction and sets `search_path` for the tenant schema.
pub async fn begin_tenant_tx(
    pool: &PgPool,
    tenant_id: &str,
) -> Result<Transaction<'static, Postgres>> {
    let schema = tenant_schema_name(tenant_id)?;
    let mut tx = pool
        .begin()
        .await
        .context("Failed to begin tenant transaction")?;

    // Use set_config to avoid identifier interpolation.
    // is_local=true => `SET LOCAL` semantics bound to the transaction.
    sqlx::query(
        r#"
        SELECT set_config('search_path', $1, true)
        "#,
    )
    .bind(format!("{schema}, public"))
    .execute(&mut *tx)
    .await
    .context("Failed to SET LOCAL search_path")?;

    Ok(tx)
}

/// Same behavior, but on a borrowed connection (useful for nested
/// composition).
pub async fn set_tenant_search_path(
    conn: &mut PgConnection,
    tenant_id: &str,
) -> Result<()> {
    let schema = tenant_schema_name(tenant_id)?;
    sqlx::query("SELECT set_config('search_path', $1, true)")
        .bind(format!("{schema}, public"))
        .execute(conn)
        .await
        .context("Failed to SET LOCAL search_path")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tenant_schema_name_validates() {
        assert_eq!(tenant_schema_name("acme").unwrap(), "tenant_acme");
        assert!(tenant_schema_name("").is_err());
        assert!(tenant_schema_name("  ").is_err());
        assert!(tenant_schema_name("evil;drop").is_err());
        assert!(tenant_schema_name("a/b").is_err());
        assert!(tenant_schema_name(&"a".repeat(65)).is_err());
    }
}
