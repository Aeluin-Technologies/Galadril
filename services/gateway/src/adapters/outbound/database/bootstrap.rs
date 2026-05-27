//! Database bootstrap.

use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use jsonwebtoken::{Algorithm, EncodingKey, Header, encode};
use pgvector::Vector;
use secrecy::ExposeSecret;
use serde::{Deserialize, Serialize};
use sqlx::PgPool;

use crate::application::usecases::authorization::AuthService;
use crate::config::AppConfig;

static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations");

pub async fn run_migrations(pool: &PgPool) -> Result<()> {
    MIGRATOR
        .run(pool)
        .await
        .context("bootstrap: sqlx migrations failed")?;
    Ok(())
}

/// Debug-only provisioning result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DebugAdminProvision {
    pub tenant_id: String,
    pub user_id: String,
    pub jwt: String,
}

/// Provisions a debug admin user in Postgres IAM tables and returns a JWT.
///
/// This does NOT seed SpiceDB. Call [`provision_debug_fixtures`] after the
/// authorization engine is initialized.
pub async fn provision_debug_admin(
    pool: &PgPool,
    cfg: &AppConfig,
) -> Result<Option<DebugAdminProvision>> {
    if !cfg!(debug_assertions) {
        return Ok(None);
    }

    let tenant_id = "debug_tenant";
    let user_id = "admin";

    let mut tx = pool.begin().await.context("debug_admin: begin tx failed")?;

    sqlx::query(
        r#"
        INSERT INTO iam_users (tenant_id, user_id, is_active)
        VALUES ($1, $2, TRUE)
        ON CONFLICT (tenant_id, user_id) DO UPDATE
        SET is_active = EXCLUDED.is_active,
            updated_at = NOW()
        "#,
    )
    .bind(tenant_id)
    .bind(user_id)
    .execute(&mut *tx)
    .await
    .context("debug_admin: upsert iam_users failed")?;

    let scope_all = serde_json::json!({ "*": true });

    sqlx::query(
        r#"
        INSERT INTO iam_user_permissions (tenant_id, user_id, effect, action, scope, updated_at)
        VALUES ($1, $2, 'allow', '*', $3, NOW())
        ON CONFLICT DO NOTHING
        "#,
    )
    .bind(tenant_id)
    .bind(user_id)
    .bind(&scope_all)
    .execute(&mut *tx)
    .await
    .context("debug_admin: insert iam_user_permissions failed")?;

    tx.commit().await.context("debug_admin: commit tx failed")?;

    let jwt = mint_debug_jwt(cfg, tenant_id, user_id)
        .context("debug_admin: mint jwt failed")?;

    Ok(Some(DebugAdminProvision {
        tenant_id: tenant_id.to_owned(),
        user_id: user_id.to_owned(),
        jwt,
    }))
}

/// Seeds a minimal dataset for search and relations testing.
/// This writes:
/// - Postgres: entity_states + entity_embeddings
/// - AGE: two vertices + one edge
/// - SpiceDB: tenant membership + entity_state parent linkage
pub async fn provision_debug_fixtures(
    pool: &PgPool,
    auth: &AuthService,
    tenant_id: &str,
    user_id: &str,
) -> Result<()> {
    if !cfg!(debug_assertions) {
        return Ok(());
    }

    let entity_id = "entity_debug_1";
    let embedding_id = "embed_debug_1";
    let modality = "vision";

    {
        let mut tx =
            pool.begin().await.context("fixtures: begin tx failed")?;

        let state_value = serde_json::json!({
            "name": "Debug Entity One",
            "zone": "debug_zone",
            "kind": "fixture"
        });

        sqlx::query(
            r#"
            INSERT INTO entity_states
                (tenant_id, entity_id, event_id, state_type, state_value, geom, event_time, ingested_at)
            VALUES
                ($1, $2, $3, $4, $5, NULL, NOW(), NOW())
            ON CONFLICT DO NOTHING
            "#,
        )
        .bind(tenant_id)
        .bind(entity_id)
        .bind("event_debug_1")
        .bind("profile")
        .bind(&state_value)
        .execute(&mut *tx)
        .await
        .context("fixtures: insert entity_states failed")?;

        let embedding = Vector::from(vec![0.0_f32; 1024]);
        let metadata =
            serde_json::json!({ "fixture": true, "source": "bootstrap" });

        sqlx::query(
            r#"
            INSERT INTO entity_embeddings
                (id, entity_id, modality, embedding, tenant_id, created_at, metadata)
            VALUES
                ($1, $2, $3, $4, $5, NOW(), $6)
            ON CONFLICT DO NOTHING
            "#,
        )
        .bind(embedding_id)
        .bind(entity_id)
        .bind(modality)
        .bind(embedding)
        .bind(tenant_id)
        .bind(&metadata)
        .execute(&mut *tx)
        .await
        .context("fixtures: insert entity_embeddings failed")?;

        // Event for event search.
        sqlx::query(
            r#"
            INSERT INTO eskg_events
                (event_id, tenant_id, event_type, event_time, properties, ingested_at)
            VALUES
                ($1, $2, $3, NOW(), $4, NOW())
            ON CONFLICT DO NOTHING
            "#,
        )
        .bind("event_debug_1")
        .bind(tenant_id)
        .bind("trigger")
        .bind(serde_json::json!({ "entity_id": entity_id, "name": "Debug trigger event" }))
        .execute(&mut *tx)
        .await
        .context("fixtures: insert eskg_events failed")?;

        tx.commit().await.context("fixtures: commit tx failed")?;
    }

    {
        let cypher = r#"
        MERGE (a:Entity {id: $id1})
        MERGE (b:Entity {id: $id2})
        MERGE (a)-[:RELATED_TO {label: "fixture"}]-(b)
        "#;

        let query = format!(
            r#"
            SELECT * FROM cypher('galadril_graph', $$
              {cypher}
            $$, $1) AS (v agtype)
            "#
        );

        let params =
            serde_json::json!({ "id1": entity_id, "id2": "entity_debug_2" });

        // If AGE isn't available, log and continue.
        if let Err(e) = sqlx::query(&query).bind(params).execute(pool).await {
            tracing::warn!(error = %e, "fixtures_age_seed_failed");
        }
    }

    auth.upsert_relationship("tenant", tenant_id, "member", "user", user_id)
        .await
        .context("fixtures: upsert tenant member relationship failed")?;

    auth.upsert_relationship(
        "entity_state",
        entity_id,
        "parent",
        "tenant",
        tenant_id,
    )
    .await
    .context("fixtures: upsert entity_state parent relationship failed")?;

    Ok(())
}

#[derive(Debug, Serialize, Deserialize)]
struct DebugClaims {
    sub: String,
    exp: usize,
    tenant_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    iss: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    aud: Option<String>,
}

fn mint_debug_jwt(
    cfg: &AppConfig,
    tenant_id: &str,
    user_id: &str,
) -> Result<String> {
    let pem = cfg.jwt.es256_private_key_pem.as_ref().ok_or_else(|| {
        anyhow::anyhow!(
            "Missing jwt.es256_private_key_pem (required for debug admin JWT)"
        )
    })?;

    let now_s = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("SystemTime before UNIX_EPOCH")?
        .as_secs();

    let exp = now_s
        .checked_add(24 * 60 * 60)
        .ok_or_else(|| anyhow::anyhow!("exp overflow"))?
        as usize;

    let claims = DebugClaims {
        sub: user_id.to_owned(),
        exp,
        tenant_id: tenant_id.to_owned(),
        iss: cfg.jwt.issuer.clone(),
        aud: cfg.jwt.audience.clone(),
    };

    let mut header = Header::new(Algorithm::ES256);
    header.typ = Some("JWT".to_owned());

    let key = EncodingKey::from_ec_pem(pem.expose_secret().as_bytes())
        .map_err(|e| anyhow::anyhow!("Invalid ES256 private key PEM: {e}"))?;

    encode(&header, &claims, &key)
        .map_err(|e| anyhow::anyhow!("JWT encode failed: {e}"))
}
