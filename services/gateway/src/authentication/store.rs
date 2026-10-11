//! Canonical identity and WebAuthn persistence with transaction-local tenant isolation.

use anyhow::{Result, ensure};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{PgPool, Postgres, Row, Transaction};

pub struct AuthStore {
    pool: PgPool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Challenge {
    pub generation: i64,
    pub challenge: String,
    pub operation: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Credential {
    pub id: String,
    pub public_key: String,
    pub counter: i64,
    #[serde(default)]
    pub transports: Vec<String>,
    pub device_type: String,
    pub backed_up: bool,
}

fn identifier(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty()
            && value.len() <= 64
            && value.bytes().all(|byte| byte.is_ascii_alphanumeric()
                || matches!(byte, b'_' | b'-')),
        "Invalid identity reference"
    );
    Ok(())
}

pub fn email_domain(email: &str) -> Result<&str> {
    ensure!(
        email.len() <= 254
            && !email.bytes().any(|byte| byte.is_ascii_whitespace()),
        "Invalid email"
    );
    let (local, domain) = email
        .split_once('@')
        .ok_or_else(|| anyhow::anyhow!("Invalid email"))?;
    ensure!(
        !local.is_empty()
            && !domain.is_empty()
            && !domain.contains('@')
            && domain.split('.').count() >= 2
            && domain.bytes().all(|byte| byte.is_ascii_alphanumeric()
                || matches!(byte, b'.' | b'-')),
        "Invalid email"
    );
    Ok(domain)
}

impl AuthStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    async fn tenant(&self, tenant: &str) -> Result<Transaction<'_, Postgres>> {
        identifier(tenant)?;
        let mut transaction = self.pool.begin().await?;
        sqlx::query("SELECT set_config('app.tenant_id', $1, true)")
            .bind(tenant)
            .execute(&mut *transaction)
            .await?;
        Ok(transaction)
    }

    pub async fn login_provider(
        &self,
        email: &str,
        local_enabled: bool,
    ) -> Result<Value> {
        let domain = email_domain(email)?.to_ascii_lowercase();
        let rows = sqlx::query("SELECT domain,tenant_id,connection_id,jit_provision FROM sso_connections WHERE domain=$1 AND enabled").bind(&domain).fetch_all(&self.pool).await?;
        if rows.is_empty() && local_enabled {
            return Ok(json!({"kind":"local"}));
        }
        ensure!(rows.len() == 1, "Registered provider required");
        let row = rows
            .first()
            .ok_or_else(|| anyhow::anyhow!("Provider missing"))?;
        Ok(json!({"kind":"sso", "connection": {
            "domain": row.try_get::<String,_>("domain")?,
            "tenant_id": row.try_get::<String,_>("tenant_id")?,
            "connection_id": row.try_get::<String,_>("connection_id")?,
            "jit_provision": row.try_get::<bool,_>("jit_provision")?,
        }}))
    }

    pub async fn active_user(
        &self,
        tenant: &str,
        user: &str,
    ) -> Result<String> {
        identifier(user)?;
        let mut transaction = self.tenant(tenant).await?;
        let value = sqlx::query_scalar("SELECT user_id FROM iam_users WHERE tenant_id=$1 AND user_id=$2 AND is_active AND deleted_at IS NULL").bind(tenant).bind(user).fetch_one(&mut *transaction).await?;
        transaction.commit().await?;
        Ok(value)
    }

    async fn profile(
        transaction: &mut Transaction<'_, Postgres>,
        tenant: &str,
        user: &str,
        profile: &Value,
    ) -> Result<()> {
        let bounded = |key, limit| {
            profile
                .get(key)
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty() && value.len() <= limit)
        };
        sqlx::query("UPDATE iam_users SET display_name=COALESCE($3,display_name),email=COALESCE($4,email) WHERE tenant_id=$1 AND user_id=$2").bind(tenant).bind(user).bind(bounded("name",128)).bind(bounded("email",254)).execute(&mut **transaction).await?;
        Ok(())
    }

    /// Only immutable provider subjects link accounts; email is display metadata.
    pub async fn resolve_identity(
        &self,
        domain: &str,
        connection: &str,
        external: &str,
        profile: &Value,
    ) -> Result<String> {
        ensure!(
            !external.is_empty() && external.len() <= 2048,
            "Invalid provider subject"
        );
        let provider = self
            .login_provider(&format!("routing@{domain}"), false)
            .await?;
        let selected = provider
            .get("connection")
            .ok_or_else(|| anyhow::anyhow!("Provider missing"))?;
        ensure!(
            selected.get("connection_id").and_then(Value::as_str)
                == Some(connection),
            "Provider changed"
        );
        let tenant = selected
            .get("tenant_id")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("Tenant missing"))?;
        let mut transaction = self.tenant(tenant).await?;
        let lock = serde_json::to_string(&(tenant, connection, external))?;
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
            .bind(lock)
            .execute(&mut *transaction)
            .await?;
        let existing = sqlx::query("SELECT i.user_id,u.is_active,u.deleted_at IS NOT NULL AS deleted FROM iam_external_identities i JOIN iam_users u USING(tenant_id,user_id) WHERE i.tenant_id=$1 AND i.connection_id=$2 AND i.external_subject=$3").bind(tenant).bind(connection).bind(external).fetch_optional(&mut *transaction).await?;
        let user = if let Some(row) = existing {
            ensure!(
                row.try_get::<bool, _>("is_active")?
                    && !row.try_get::<bool, _>("deleted")?,
                "Inactive identity"
            );
            row.try_get::<String, _>("user_id")?
        } else {
            ensure!(
                selected.get("jit_provision").and_then(Value::as_bool)
                    == Some(true),
                "Provisioned identity required"
            );
            let user = uuid::Uuid::new_v4().to_string();
            sqlx::query(
                "INSERT INTO iam_users(tenant_id,user_id) VALUES($1,$2)",
            )
            .bind(tenant)
            .bind(&user)
            .execute(&mut *transaction)
            .await?;
            sqlx::query("INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id) VALUES($1,$2,$3,$4)").bind(tenant).bind(connection).bind(external).bind(&user).execute(&mut *transaction).await?;
            user
        };
        Self::profile(&mut transaction, tenant, &user, profile).await?;
        transaction.commit().await?;
        Ok(user)
    }

    pub async fn bind_local(
        &self,
        tenant: &str,
        user: &str,
        external: &str,
        profile: &Value,
    ) -> Result<()> {
        identifier(user)?;
        let mut transaction = self.tenant(tenant).await?;
        let exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM iam_external_identities i JOIN iam_users u USING(tenant_id,user_id) WHERE i.tenant_id=$1 AND i.user_id=$2 AND i.connection_id='kratos' AND i.external_subject=$3 AND u.is_active AND u.deleted_at IS NULL)").bind(tenant).bind(user).bind(external).fetch_one(&mut *transaction).await?;
        ensure!(exists, "Provisioned local identity required");
        Self::profile(&mut transaction, tenant, user, profile).await?;
        transaction.commit().await?;
        Ok(())
    }

    pub async fn passkey_context(
        &self,
        tenant: &str,
        user: &str,
    ) -> Result<Value> {
        identifier(user)?;
        let mut transaction = self.tenant(tenant).await?;
        let enabled: bool = sqlx::query_scalar("SELECT COALESCE(s.passkey_mfa,false) FROM iam_users u LEFT JOIN sso_tenant_settings s USING(tenant_id) WHERE u.tenant_id=$1 AND u.user_id=$2 AND u.is_active AND u.deleted_at IS NULL").bind(tenant).bind(user).fetch_one(&mut *transaction).await?;
        if !enabled {
            transaction.commit().await?;
            return Ok(json!({"enabled":false}));
        }
        sqlx::query("INSERT INTO iam_passkey_state(tenant_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING").bind(tenant).bind(user).execute(&mut *transaction).await?;
        let state = sqlx::query("SELECT generation,user_handle FROM iam_passkey_state WHERE tenant_id=$1 AND user_id=$2").bind(tenant).bind(user).fetch_one(&mut *transaction).await?;
        let keys = sqlx::query("SELECT credential_id,public_key,counter,transports,device_type,backed_up FROM iam_passkeys WHERE tenant_id=$1 AND user_id=$2").bind(tenant).bind(user).fetch_all(&mut *transaction).await?;
        let mut credentials = Vec::with_capacity(keys.len());
        for key in keys {
            credentials.push(json!({
            "id":key.try_get::<String,_>("credential_id")?,
            "publicKey":URL_SAFE_NO_PAD.encode(key.try_get::<Vec<u8>,_>("public_key")?),
            "counter":key.try_get::<i64,_>("counter")?,
            "transports":key.try_get::<Vec<String>,_>("transports")?,
            "deviceType":key.try_get::<String,_>("device_type")?,
            "backedUp":key.try_get::<bool,_>("backed_up")?,
        }));
        }
        let result = json!({"enabled":true,"generation":state.try_get::<i64,_>("generation")?,"userHandle":URL_SAFE_NO_PAD.encode(state.try_get::<Vec<u8>,_>("user_handle")?),"credentials":credentials});
        transaction.commit().await?;
        Ok(result)
    }

    async fn lock(
        transaction: &mut Transaction<'_, Postgres>,
        tenant: &str,
        user: &str,
        expected: i64,
    ) -> Result<()> {
        ensure!(
            (0..=9_007_199_254_740_991).contains(&expected),
            "Invalid generation"
        );
        let generation: i64 = sqlx::query_scalar("SELECT k.generation FROM iam_passkey_state k JOIN iam_users u USING(tenant_id,user_id) JOIN sso_tenant_settings s USING(tenant_id) WHERE k.tenant_id=$1 AND k.user_id=$2 AND u.is_active AND u.deleted_at IS NULL AND s.passkey_mfa FOR UPDATE OF k").bind(tenant).bind(user).fetch_one(&mut **transaction).await?;
        ensure!(generation == expected, "Credential generation changed");
        Ok(())
    }

    pub async fn challenge(
        &self,
        tenant: &str,
        user: &str,
        flow: &str,
        value: &Challenge,
    ) -> Result<()> {
        ensure!(
            flow.len() == 43
                && value.challenge.len() <= 1024
                && matches!(
                    value.operation.as_str(),
                    "registration" | "authentication"
                ),
            "Invalid challenge"
        );
        let mut transaction = self.tenant(tenant).await?;
        Self::lock(&mut transaction, tenant, user, value.generation).await?;
        sqlx::query("DELETE FROM iam_passkey_challenges WHERE tenant_id=$1 AND expires_at<=now()").bind(tenant).execute(&mut *transaction).await?;
        let inserted = sqlx::query("INSERT INTO iam_passkey_challenges(flow_id,tenant_id,user_id,generation,challenge,operation) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(flow_id) DO UPDATE SET generation=EXCLUDED.generation,challenge=EXCLUDED.challenge,operation=EXCLUDED.operation,expires_at=now()+interval '5 minutes' WHERE iam_passkey_challenges.tenant_id=EXCLUDED.tenant_id AND iam_passkey_challenges.user_id=EXCLUDED.user_id").bind(flow).bind(tenant).bind(user).bind(value.generation).bind(&value.challenge).bind(&value.operation).execute(&mut *transaction).await?;
        ensure!(inserted.rows_affected() == 1, "Challenge binding changed");
        transaction.commit().await?;
        Ok(())
    }

    /// Consumption commits separately, so failed verification cannot replay a challenge.
    pub async fn consume(
        &self,
        tenant: &str,
        user: &str,
        flow: &str,
    ) -> Result<Value> {
        let mut transaction = self.tenant(tenant).await?;
        let row = sqlx::query("DELETE FROM iam_passkey_challenges WHERE tenant_id=$1 AND user_id=$2 AND flow_id=$3 AND expires_at>now() RETURNING generation,challenge,operation").bind(tenant).bind(user).bind(flow).fetch_one(&mut *transaction).await?;
        let result = json!({"generation":row.try_get::<i64,_>("generation")?,"challenge":row.try_get::<String,_>("challenge")?,"operation":row.try_get::<String,_>("operation")?});
        transaction.commit().await?;
        Ok(result)
    }

    /// Row locks serialize recovery against counter updates and first enrollment.
    pub async fn save(
        &self,
        tenant: &str,
        user: &str,
        generation: i64,
        value: &Credential,
        previous: Option<i64>,
    ) -> Result<()> {
        ensure!(
            !value.id.is_empty()
                && value.id.len() <= 2048
                && (0..=4_294_967_295).contains(&value.counter),
            "Invalid credential"
        );
        let mut transaction = self.tenant(tenant).await?;
        Self::lock(&mut transaction, tenant, user, generation).await?;
        if let Some(previous) = previous {
            let updated = sqlx::query("UPDATE iam_passkeys SET counter=$4,backed_up=$5,last_used_at=now() WHERE tenant_id=$1 AND user_id=$2 AND credential_id=$3 AND counter=$6").bind(tenant).bind(user).bind(&value.id).bind(value.counter).bind(value.backed_up).bind(previous).execute(&mut *transaction).await?;
            ensure!(updated.rows_affected() == 1, "Credential changed");
        } else {
            let count: i64 = sqlx::query_scalar("SELECT count(*) FROM iam_passkeys WHERE tenant_id=$1 AND user_id=$2").bind(tenant).bind(user).fetch_one(&mut *transaction).await?;
            ensure!(count == 0, "Credential already enrolled");
            let key = URL_SAFE_NO_PAD.decode(&value.public_key)?;
            ensure!(
                !key.is_empty()
                    && key.len() <= 8192
                    && value.transports.len() <= 8,
                "Invalid public key"
            );
            sqlx::query("INSERT INTO iam_passkeys(tenant_id,user_id,credential_id,public_key,counter,transports,device_type,backed_up) VALUES($1,$2,$3,$4,$5,$6,$7,$8)").bind(tenant).bind(user).bind(&value.id).bind(key).bind(value.counter).bind(&value.transports).bind(&value.device_type).bind(value.backed_up).execute(&mut *transaction).await?;
        }
        transaction.commit().await?;
        Ok(())
    }
}
