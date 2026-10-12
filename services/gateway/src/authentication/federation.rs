//! Opt-in federation membership reconciliation without external administrator
//! grants.

use std::sync::Arc;

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::postgres::{PgAdvisoryLock, PgAdvisoryLockKey};
use sqlx::{Connection as _, PgPool, Postgres, Transaction};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FederationMapping {
    pub external_group: String,
    pub role_name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FederationConfig {
    pub connection_id: String,
    #[serde(default = "default_claim")]
    pub claim: String,
    #[serde(default)]
    pub mappings: Vec<FederationMapping>,
    #[serde(default)]
    pub enabled: bool,
}

fn default_claim() -> String {
    "raw.groups".to_owned()
}

impl FederationConfig {
    /// Bounds provider-controlled values before they reach authorization
    /// storage.
    pub fn validate(&self) -> Result<()> {
        ensure!(
            !self.connection_id.is_empty() &&
                self.connection_id.len() <= 2048 &&
                !self.connection_id.chars().any(char::is_control),
            "Choose a valid sign-in connection"
        );
        ensure!(
            !self.claim.is_empty() &&
                self.claim.len() <= 128 &&
                self.claim.split('.').count() <= 4 &&
                self.claim.split('.').all(|part| !part.is_empty() &&
                    part.bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() ||
                            matches!(byte, b'_' | b'-'))),
            "Choose a valid groups or roles claim"
        );
        ensure!(
            self.mappings.len() <= 64 &&
                (!self.enabled || !self.mappings.is_empty()),
            "Configure between 1 and 64 group mappings before enabling synchronization"
        );
        for mapping in &self.mappings {
            ensure!(
                !mapping.external_group.is_empty() &&
                    mapping.external_group.len() <= 256 &&
                    !mapping.external_group.chars().any(char::is_control),
                "External group names must contain between 1 and 256 printable characters"
            );
            let role = &mapping.role_name;
            ensure!(
                !role.is_empty() &&
                    role.len() <= 64 &&
                    role.bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() ||
                            matches!(byte, b'_' | b'-')),
                "Choose a role belonging to this organization"
            );
            ensure!(
                !["admin", "administrator", "owner", "organization_admin"]
                    .iter()
                    .any(|reserved| role.eq_ignore_ascii_case(reserved)),
                "Organization administrator roles cannot be assigned by identity-provider synchronization"
            );
        }
        Ok(())
    }
}

/// Accepts only the administrator-selected claim from verified provider
/// userinfo.
fn desired_roles<'a>(
    config: &'a FederationConfig,
    profile: &Value,
) -> Result<Vec<&'a str>> {
    config.validate()?;
    if !config.enabled {
        return Ok(Vec::new());
    }
    let claim = config.claim.split('.').try_fold(profile, |value, part| value.get(part))
        .and_then(Value::as_array).ok_or_else(|| anyhow::anyhow!("The identity provider did not return the configured groups claim"))?;
    ensure!(
        claim.len() <= 256,
        "The identity provider returned too many groups"
    );
    for group in claim {
        ensure!(
            group.as_str().is_some_and(|value| !value.is_empty() &&
                value.len() <= 256 &&
                !value.chars().any(char::is_control)),
            "The identity provider returned an invalid groups claim"
        );
    }
    let mut desired = Vec::with_capacity(config.mappings.len());
    for mapping in &config.mappings {
        if claim.iter().any(|group| {
            group.as_str() == Some(mapping.external_group.as_str())
        }) && !desired.contains(&mapping.role_name.as_str())
        {
            desired.push(mapping.role_name.as_str());
        }
    }
    Ok(desired)
}

#[async_trait::async_trait]
pub trait MembershipWriter: Send + Sync {
    /// Updates only a tenant-qualified role member relation, never
    /// administrators.
    async fn role_membership(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
        enabled: bool,
    ) -> Result<()>;
    /// Adds the canonical member relation required for delegated resource
    /// access.
    async fn tenant_membership(&self, tenant: &str, user: &str) -> Result<()>;
}

/// Reads only secret-free mapping configuration through an existing RLS
/// transaction.
pub async fn configurations(
    transaction: &mut Transaction<'_, Postgres>,
    tenant: &str,
) -> Result<Vec<FederationConfig>> {
    let values: Vec<Value> = sqlx::query_scalar("SELECT configuration FROM sso_role_sync_configs WHERE tenant_id=$1 ORDER BY connection_id LIMIT 64")
        .bind(tenant).fetch_all(&mut **transaction).await?;
    values
        .into_iter()
        .map(|value| serde_json::from_value(value).map_err(Into::into))
        .collect()
}

/// Requires explicit tenant opt-in and existing roles before publishing
/// mappings.
pub async fn configure(
    transaction: &mut Transaction<'_, Postgres>,
    tenant: &str,
    config: &FederationConfig,
) -> Result<()> {
    config.validate()?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,713059))")
        .bind(tenant)
        .execute(&mut **transaction)
        .await?;
    let lock = serde_json::to_string(&(tenant, &config.connection_id))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,921743))")
        .bind(lock)
        .execute(&mut **transaction)
        .await?;
    let enabled: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM sso_tenant_settings WHERE tenant_id=$1 AND permission_import)").bind(tenant).fetch_one(&mut **transaction).await?;
    ensure!(
        !config.enabled || enabled,
        "Enable permission import for this organization before enabling role synchronization"
    );
    let connection: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM sso_connections WHERE tenant_id=$1 AND connection_id=$2 AND enabled)").bind(tenant).bind(&config.connection_id).fetch_one(&mut **transaction).await?;
    ensure!(
        connection,
        "Choose an active sign-in connection belonging to this organization"
    );
    for mapping in &config.mappings {
        let role: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM iam_roles WHERE tenant_id=$1 AND role_name=$2 AND deleted_at IS NULL)").bind(tenant).bind(&mapping.role_name).fetch_one(&mut **transaction).await?;
        ensure!(
            role,
            "Create the selected organization role before mapping an external group"
        );
    }
    sqlx::query("INSERT INTO sso_role_sync_configs(tenant_id,connection_id,configuration) VALUES($1,$2,$3) ON CONFLICT(tenant_id,connection_id) DO UPDATE SET configuration=EXCLUDED.configuration,updated_at=now()")
        .bind(tenant).bind(&config.connection_id).bind(serde_json::to_value(config)?).execute(&mut **transaction).await?;
    Ok(())
}

/// Reconciles imported sources while preserving independent manual
/// assignments.
pub async fn synchronize(
    pool: &PgPool,
    writer: Option<&Arc<dyn MembershipWriter>>,
    tenant: &str,
    user: &str,
    connection: &str,
    profile: &Value,
) -> Result<()> {
    let mut database_connection = pool.acquire().await?;
    let key: i64 = sqlx::query_scalar("SELECT hashtextextended($1,713059)")
        .bind(tenant)
        .fetch_one(&mut *database_connection)
        .await?;
    // SQLx releases this session lock on cancellation and pool return. Keeping
    // it across commit serializes role deletion against external projection.
    let lock = PgAdvisoryLock::with_key(PgAdvisoryLockKey::BigInt(key));
    let mut database_connection = lock.acquire(database_connection).await?;
    let mut transaction = database_connection.begin().await?;
    sqlx::query("SELECT set_config('app.tenant_id',$1,true)")
        .bind(tenant)
        .execute(&mut *transaction)
        .await?;
    let lock = serde_json::to_string(&(tenant, connection))?;
    sqlx::query(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended($1,921743))",
    )
    .bind(lock)
    .execute(&mut *transaction)
    .await?;
    let config: Option<Value> = sqlx::query_scalar("SELECT configuration FROM sso_role_sync_configs WHERE tenant_id=$1 AND connection_id=$2").bind(tenant).bind(connection).fetch_optional(&mut *transaction).await?;
    let Some(config) = config else {
        transaction.commit().await?;
        return Ok(());
    };
    let config: FederationConfig = serde_json::from_value(config)?;
    let enabled: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM sso_tenant_settings WHERE tenant_id=$1 AND permission_import)").bind(tenant).fetch_one(&mut *transaction).await?;
    // Pausing import preserves prior approval; removing groups explicitly
    // revokes.
    if !config.enabled || !enabled {
        transaction.commit().await?;
        return Ok(());
    }
    let desired = desired_roles(&config, profile)?;
    let writer = writer.ok_or_else(|| {
        anyhow::anyhow!(
            "Identity-provider role synchronization is not available"
        )
    })?;
    let lock = serde_json::to_string(&(tenant, user))?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,921744))")
        .bind(lock)
        .execute(&mut *transaction)
        .await?;
    // Revocation precedes persistence; dependency failure cannot retain
    // revoked access.
    sqlx::query("DELETE FROM iam_federated_role_assignments WHERE tenant_id=$1 AND user_id=$2 AND connection_id=$3 AND NOT(role_name=ANY($4))").bind(tenant).bind(user).bind(connection).bind(&desired).execute(&mut *transaction).await?;
    let revoked: Vec<String> = sqlx::query_scalar("DELETE FROM iam_user_roles r WHERE tenant_id=$1 AND user_id=$2 AND NOT manual AND NOT EXISTS(SELECT FROM iam_federated_role_assignments f WHERE f.tenant_id=r.tenant_id AND f.user_id=r.user_id AND f.role_name=r.role_name) RETURNING role_name").bind(tenant).bind(user).fetch_all(&mut *transaction).await?;
    for role in &revoked {
        writer.role_membership(tenant, user, role, false).await?;
    }
    for role in &desired {
        let current: bool = sqlx::query_scalar("SELECT EXISTS(SELECT FROM iam_roles WHERE tenant_id=$1 AND role_name=$2 AND deleted_at IS NULL)").bind(tenant).bind(role).fetch_one(&mut *transaction).await?;
        ensure!(
            current,
            "A mapped role was removed; ask an organization administrator to update the mapping"
        );
        sqlx::query("INSERT INTO iam_user_roles(tenant_id,user_id,role_name,manual) VALUES($1,$2,$3,false) ON CONFLICT DO NOTHING").bind(tenant).bind(user).bind(role).execute(&mut *transaction).await?;
        sqlx::query("INSERT INTO iam_federated_role_assignments(tenant_id,user_id,connection_id,role_name) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING").bind(tenant).bind(user).bind(connection).bind(role).execute(&mut *transaction).await?;
    }
    transaction.commit().await?;
    for role in &desired {
        writer.role_membership(tenant, user, role, true).await?;
    }
    let current = async {
        let mut transaction = database_connection.begin().await?;
        sqlx::query("SELECT set_config('app.tenant_id',$1,true)")
            .bind(tenant)
            .execute(&mut *transaction)
            .await?;
        let roles: Vec<String> = sqlx::query_scalar("SELECT a.role_name FROM iam_user_roles a JOIN iam_roles r USING(tenant_id,role_name) JOIN iam_users u USING(tenant_id,user_id) WHERE a.tenant_id=$1 AND a.user_id=$2 AND a.role_name=ANY($3) AND r.deleted_at IS NULL AND u.deleted_at IS NULL AND u.is_active")
            .bind(tenant).bind(user).bind(&desired).fetch_all(&mut *transaction).await?;
        transaction.commit().await?;
        Ok::<_, anyhow::Error>(roles)
    }.await;
    let current = match current {
        Ok(current) => current,
        Err(error) => {
            for role in &desired {
                writer.role_membership(tenant, user, role, false).await?;
            }
            return Err(error);
        },
    };
    let mut changed = false;
    for role in &desired {
        if !current.iter().any(|current| current == role) {
            writer.role_membership(tenant, user, role, false).await?;
            changed = true;
        }
    }
    ensure!(
        !changed,
        "Your organization access changed during sign-in; please sign in again"
    );
    database_connection.release_now().await?;
    tracing::info!(
        event.name = "authentication.federation.roles_synchronized",
        assigned = desired.len(),
        revoked = revoked.len(),
        "provider role mappings reconciled"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use anyhow::Result;
    use serde_json::json;

    use super::*;

    #[test]
    fn omitted_claim_uses_verified_polis_upstream_groups() -> Result<()> {
        let config: FederationConfig = serde_json::from_value(json!({
            "connectionId": "trusted-provider", "enabled": true,
            "mappings": [{"externalGroup": "Finance", "roleName": "analyst"}]
        }))?;
        assert_eq!(config.claim, "raw.groups");
        assert_eq!(
            desired_roles(&config, &json!({"raw": {"groups": ["Finance"]}}))?,
            vec!["analyst"]
        );
        assert!(
            desired_roles(&config, &json!({"groups": ["Finance"]})).is_err()
        );
        Ok(())
    }

    #[test]
    fn only_explicit_mappings_from_the_selected_claim_assign_roles()
    -> Result<()> {
        let config: FederationConfig = serde_json::from_value(json!({
            "connectionId": "trusted-provider", "claim": "raw.groups", "enabled": true,
            "mappings": [{"externalGroup": "Finance", "roleName": "analyst"}, {"externalGroup": "Operations", "roleName": "operator"}]
        }))?;
        config.validate()?;
        let profile = json!({"groups":["Operations"],"raw":{"groups":["Finance", "Finance", "administrator"]}});
        assert_eq!(desired_roles(&config, &profile)?, vec!["analyst"]);
        Ok(())
    }

    #[test]
    fn missing_or_malformed_claims_do_not_silently_revoke_access() -> Result<()>
    {
        let config: FederationConfig = serde_json::from_value(json!({
            "connectionId":"provider", "claim":"groups", "enabled":true,
            "mappings":[{"externalGroup":"Finance", "roleName":"analyst"}]
        }))?;
        for profile in [
            json!({}),
            json!({"groups":"Finance"}),
            json!({"groups":["Finance",12]}),
        ] {
            assert!(desired_roles(&config, &profile).is_err());
        }
        assert!(desired_roles(&config, &json!({"groups":[]}))?.is_empty());
        Ok(())
    }

    #[test]
    fn mappings_are_bounded_and_cannot_request_organization_administrator_roles()
    -> Result<()> {
        let valid = json!({"connectionId":"provider", "enabled":true,"mappings":[{"externalGroup":"Finance","roleName":"analyst"}]});
        for role in ["admin", "Administrator", "owner", "other/analyst", ""] {
            let mut value = valid.clone();
            value["mappings"][0]["roleName"] = json!(role);
            assert!(
                serde_json::from_value::<FederationConfig>(value)?
                    .validate()
                    .is_err()
            );
        }
        let mut oversized = valid;
        oversized["mappings"] = json!((0..65).map(|index| json!({"externalGroup":format!("Group{index}"),"roleName":"analyst"})).collect::<Vec<_>>());
        assert!(
            serde_json::from_value::<FederationConfig>(oversized)?
                .validate()
                .is_err()
        );
        Ok(())
    }
}
