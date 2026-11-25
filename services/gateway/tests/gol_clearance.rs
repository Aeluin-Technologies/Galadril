//! IFC clearance comes from a live authority record, never organizational
//! roles.

use anyhow::{Context, Result};
use galadril_ontology::Classification;
use testcontainers_modules::postgres::Postgres;
use testcontainers_modules::testcontainers::ImageExt;
use testcontainers_modules::testcontainers::runners::AsyncRunner;

use super::*;

#[tokio::test]
async fn clearance_is_explicit_live_tenant_scoped_and_not_application_writable()
-> Result<()> {
    let container=Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;".to_vec()).with_tag("18.6-alpine").start().await?;
    let host = container.get_host().await?;
    let port = container.get_host_port_ipv4(5432).await?;
    let app_url =
        format!("postgres://galadril_app:galadril_app@{host}:{port}/postgres");
    let database = Database::connect(&app_url).await?;
    let store = PgIamStore::new(database.clone());
    for tenant in ["tenant_a", "tenant_b"] {
        store.create_user(tenant, "alice", true).await?;
    }
    store.create_role("tenant_a", "administrator").await?;
    store
        .assign_role_to_user("tenant_a", "alice", "administrator")
        .await?;
    assert!(store.gol_clearance("tenant_a", "alice").await?.is_none());
    let authority = sqlx::PgPool::connect(&format!(
        "postgres://postgres:postgres@{host}:{port}/postgres"
    ))
    .await?;
    sqlx::query("INSERT INTO iam_ifc_clearances(tenant_id,user_id,classification,compartments) VALUES('tenant_a','alice',2,ARRAY['Legal.Cases'])").execute(&authority).await?;
    let clearance = store
        .gol_clearance("tenant_a", "alice")
        .await?
        .context("explicit clearance missing")?;
    assert_eq!(clearance.classification, Classification::Confidential);
    assert!(clearance.compartments.contains(&"Legal.Cases".parse()?));
    assert!(store.gol_clearance("tenant_b", "alice").await?.is_none());
    assert!(store.gol_clearance("tenant_a", "unknown").await?.is_none());
    let mut transaction = database.tenant("tenant_b").await?;
    let leaked: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM iam_ifc_clearances WHERE tenant_id='tenant_a'",
    )
    .fetch_one(&mut *transaction)
    .await?;
    assert_eq!(leaked, 0);
    assert!(sqlx::query("INSERT INTO iam_ifc_clearances(tenant_id,user_id,classification,compartments) VALUES('tenant_b','alice',3,ARRAY[]::text[])").execute(&mut *transaction).await.is_err());
    transaction.rollback().await?;
    let unscoped = sqlx::PgPool::connect(&app_url).await?;
    let visible: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM iam_ifc_clearances")
            .fetch_one(&unscoped)
            .await?;
    assert_eq!(visible, 0);
    sqlx::query("UPDATE iam_users SET is_active=FALSE WHERE tenant_id='tenant_a' AND user_id='alice'").execute(&authority).await?;
    assert!(store.gol_clearance("tenant_a", "alice").await?.is_none());
    sqlx::query("UPDATE iam_users SET is_active=TRUE WHERE tenant_id='tenant_a' AND user_id='alice'").execute(&authority).await?;
    sqlx::query("UPDATE iam_ifc_clearances SET compartments=ARRAY['not a qualified name'] WHERE tenant_id='tenant_a' AND user_id='alice'").execute(&authority).await?;
    assert!(store.gol_clearance("tenant_a", "alice").await.is_err());
    sqlx::query("DELETE FROM iam_ifc_clearances WHERE tenant_id='tenant_a' AND user_id='alice'").execute(&authority).await?;
    assert!(store.gol_clearance("tenant_a", "alice").await?.is_none());
    authority.close().await;
    unscoped.close().await;
    container.stop().await?;
    Ok(())
}
