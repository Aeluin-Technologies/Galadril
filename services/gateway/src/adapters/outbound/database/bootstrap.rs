//! Database bootstrap.

use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use jsonwebtoken::{Algorithm, EncodingKey, Header, encode};
use secrecy::ExposeSecret;
use serde::{Deserialize, Serialize};
use sqlx::PgPool;

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
