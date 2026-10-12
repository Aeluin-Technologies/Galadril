//! PostgreSQL implementation of IAM persistence.
//!
//! SECURITY: Every query is explicitly tenant-scoped. Never write rows without
//! `tenant_id = $1` constraints.

use anyhow::{Context, Result, bail};
use sqlx::{Connection as _, Row};

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::access_store::{AccessStore, AccessTuple};
use crate::application::ports::iam_store::{
    CedarPolicy, IamRole, IamStore, IamUser, RoleAssignment,
};

const HARD_LIMIT: usize = 100;
const TENANT_ROLE_LIMIT: i64 = 256;
const TENANT_POLICY_LIMIT: usize = 128;
const POLICY_CONTENT_LIMIT: usize = 8192;
const TENANT_POLICY_CONTENT_LIMIT: usize = 1024 * 1024;

pub struct PgIamStore {
    database: Database,
}

impl PgIamStore {
    /// Creates an IAM store over the shared RLS-aware database pool.
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Converts PostgreSQL timestamps to Unix milliseconds.
    fn to_ms(value: sqlx::types::time::OffsetDateTime) -> i64 {
        value.unix_timestamp() * 1000 +
            i64::from(value.nanosecond()) / 1_000_000
    }

    async fn role_deletion_users(
        tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
        tenant: &str,
        role: &str,
    ) -> Result<Vec<String>> {
        let state = sqlx::query("SELECT EXISTS(SELECT FROM iam_roles WHERE tenant_id=$1 AND role_name=$2 AND deleted_at IS NULL) AS live, EXISTS(SELECT FROM iam_role_parents WHERE tenant_id=$1 AND (role_name=$2 OR parent_role_name=$2)) AS hierarchy, EXISTS(SELECT FROM sso_role_sync_configs WHERE tenant_id=$1 AND configuration->'mappings' @> jsonb_build_array(jsonb_build_object('roleName',$2::text))) OR EXISTS(SELECT FROM iam_federated_role_assignments WHERE tenant_id=$1 AND role_name=$2) AS managed")
            .bind(tenant).bind(role).fetch_one(&mut **tx).await?;
        anyhow::ensure!(
            state.try_get::<bool, _>("live")?,
            "Role is unavailable"
        );
        anyhow::ensure!(
            !state.try_get::<bool, _>("hierarchy")?,
            "Remove this role's hierarchy connections before deleting it"
        );
        anyhow::ensure!(
            !state.try_get::<bool, _>("managed")?,
            "Remove this role's identity-provider mappings and imported assignments before deleting it"
        );
        let users:Vec<String> = sqlx::query_scalar("SELECT user_id FROM iam_user_roles WHERE tenant_id=$1 AND role_name=$2 ORDER BY user_id LIMIT 101")
            .bind(tenant).bind(role).fetch_all(&mut **tx).await?;
        anyhow::ensure!(
            users.len() <= HARD_LIMIT,
            "Remove role assignments before deleting a role with more than 100 members"
        );
        Ok(users)
    }

    async fn delete_role_state(
        &self,
        tenant: &str,
        role: &str,
        access: Option<&dyn AccessStore>,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,713059))",
        )
        .bind(tenant)
        .execute(&mut *tx)
        .await?;
        let users = Self::role_deletion_users(&mut tx, tenant, role).await?;
        if let Some(access) = access {
            let mut changes = Vec::with_capacity(users.len() + 2);
            for user in users {
                changes.push(AccessTuple {
                    resource_type: "role".into(),
                    resource_id: role.into(),
                    relation: "member".into(),
                    subject_type: "user".into(),
                    subject_id: user,
                    subject_relation: String::new(),
                });
            }
            changes.push(AccessTuple {
                resource_type: "tenant".into(),
                resource_id: tenant.into(),
                relation: "role".into(),
                subject_type: "role".into(),
                subject_id: role.into(),
                subject_relation: String::new(),
            });
            changes.push(AccessTuple {
                resource_type: "role".into(),
                resource_id: role.into(),
                relation: "parent".into(),
                subject_type: "tenant".into(),
                subject_id: tenant.into(),
                subject_relation: String::new(),
            });
            // Revocation stays inside the lifecycle lock; tombstones cannot
            // race a late federated or manual membership projection.
            access.set_many(tenant, &changes, false).await?;
        }
        sqlx::query(
            "DELETE FROM iam_user_roles WHERE tenant_id=$1 AND role_name=$2",
        )
        .bind(tenant)
        .bind(role)
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE iam_roles SET deleted_at=now(),updated_at=now() WHERE tenant_id=$1 AND role_name=$2 AND deleted_at IS NULL")
            .bind(tenant).bind(role).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    async fn update_role_membership(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
        enabled: bool,
        access: Option<&dyn AccessStore>,
    ) -> Result<()> {
        let mut connection = self.database.tenant_lifecycle(tenant).await?;
        let mut tx = connection.begin().await?;
        sqlx::query("SELECT set_config('app.tenant_id',$1,true)")
            .bind(tenant)
            .execute(&mut *tx)
            .await?;
        if enabled {
            let result = sqlx::query("INSERT INTO iam_user_roles(tenant_id,user_id,role_name,manual) SELECT $1,$2,$3,true WHERE EXISTS(SELECT FROM iam_users WHERE tenant_id=$1 AND user_id=$2 AND is_active AND deleted_at IS NULL) AND EXISTS(SELECT FROM iam_roles WHERE tenant_id=$1 AND role_name=$3 AND deleted_at IS NULL) ON CONFLICT(tenant_id,user_id,role_name) DO UPDATE SET manual=true")
                .bind(tenant).bind(user).bind(role).execute(&mut *tx).await?;
            anyhow::ensure!(
                result.rows_affected() == 1,
                "Active user and role are required for assignment"
            );
        } else {
            let imported:bool=sqlx::query_scalar("SELECT EXISTS(SELECT FROM iam_federated_role_assignments WHERE tenant_id=$1 AND user_id=$2 AND role_name=$3)")
                .bind(tenant).bind(user).bind(role).fetch_one(&mut *tx).await?;
            anyhow::ensure!(
                !imported,
                "This role is managed by your identity provider; change its group mapping first"
            );
            sqlx::query("DELETE FROM iam_user_roles WHERE tenant_id=$1 AND user_id=$2 AND role_name=$3")
                .bind(tenant).bind(user).bind(role).execute(&mut *tx).await?;
        }
        let tuple = AccessTuple {
            resource_type: "role".into(),
            resource_id: role.into(),
            relation: "member".into(),
            subject_type: "user".into(),
            subject_id: user.into(),
            subject_relation: String::new(),
        };
        if enabled {
            // Approved membership must survive a crash before native grant;
            // repeating the idempotent assignment safely completes projection.
            tx.commit().await?;
            if let Some(access) = access {
                access.set(tenant, &tuple, true).await?;
            }
        } else {
            if let Some(access) = access {
                access.set(tenant, &tuple, false).await?;
            }
            tx.commit().await?;
        }
        connection.release_now().await?;
        Ok(())
    }

    async fn update_role_parent(
        &self,
        tenant_id: &str,
        role_name: &str,
        parent_role_name: &str,
        enabled: bool,
        access: Option<
            &dyn crate::application::ports::access_store::AccessStore,
        >,
    ) -> Result<()> {
        anyhow::ensure!(
            role_name != parent_role_name,
            "A role cannot inherit itself"
        );
        let mut connection = self.database.tenant_lifecycle(tenant_id).await?;
        let mut tx = connection.begin().await?;
        sqlx::query("SELECT set_config('app.tenant_id',$1,true)")
            .bind(tenant_id)
            .execute(&mut *tx)
            .await?;
        if enabled {
            let valid:bool=sqlx::query_scalar("SELECT COUNT(*) = 2 FROM iam_roles WHERE tenant_id=$1 AND role_name IN ($2,$3) AND deleted_at IS NULL")
                .bind(tenant_id).bind(role_name).bind(parent_role_name).fetch_one(&mut *tx).await?;
            anyhow::ensure!(
                valid,
                "Both roles must exist in this organization"
            );
            let unchanged:bool=sqlx::query_scalar("SELECT NOT EXISTS(SELECT 1 FROM iam_role_parents WHERE tenant_id=$1 AND role_name=$2 AND parent_role_name<>$3)")
                .bind(tenant_id).bind(role_name).bind(parent_role_name).fetch_one(&mut *tx).await?;
            anyhow::ensure!(
                unchanged,
                "Remove the existing parent role before choosing another"
            );
            let cycle:bool=sqlx::query_scalar(r#"
                WITH RECURSIVE ancestors(role_name) AS (
                    SELECT $3::text UNION SELECT edge.parent_role_name FROM iam_role_parents edge JOIN ancestors ON edge.role_name=ancestors.role_name WHERE edge.tenant_id=$1
                ) SELECT EXISTS(SELECT 1 FROM ancestors WHERE role_name=$2)
            "#).bind(tenant_id).bind(role_name).bind(parent_role_name).fetch_one(&mut *tx).await?;
            anyhow::ensure!(!cycle, "Role hierarchy cannot contain a cycle");
            // SpiceDB must keep sufficient recursion budget for document and
            // tenant checks after expanding a deliberately bounded hierarchy.
            let shallow:bool=sqlx::query_scalar(r#"
                WITH RECURSIVE ancestors(role_name,depth) AS (
                    SELECT $3::text,0 UNION ALL SELECT edge.parent_role_name,depth+1 FROM iam_role_parents edge JOIN ancestors ON edge.role_name=ancestors.role_name WHERE edge.tenant_id=$1
                ), descendants(role_name,depth) AS (
                    SELECT $2::text,0 UNION ALL SELECT edge.role_name,depth+1 FROM iam_role_parents edge JOIN descendants ON edge.parent_role_name=descendants.role_name WHERE edge.tenant_id=$1
                ) SELECT (SELECT MAX(depth) FROM ancestors)+(SELECT MAX(depth) FROM descendants)+1 <= 16
            "#).bind(tenant_id).bind(role_name).bind(parent_role_name).fetch_one(&mut *tx).await?;
            anyhow::ensure!(
                shallow,
                "Role hierarchy must contain at most 16 levels"
            );
            sqlx::query("INSERT INTO iam_role_parents(tenant_id,role_name,parent_role_name) VALUES($1,$2,$3) ON CONFLICT(tenant_id,role_name) DO NOTHING")
                .bind(tenant_id).bind(role_name).bind(parent_role_name).execute(&mut *tx).await?;
        } else {
            sqlx::query("DELETE FROM iam_role_parents WHERE tenant_id=$1 AND role_name=$2 AND parent_role_name=$3")
                .bind(tenant_id).bind(role_name).bind(parent_role_name).execute(&mut *tx).await?;
        }
        let tuple = crate::application::ports::access_store::AccessTuple {
            resource_type: "role".into(),
            resource_id: role_name.into(),
            relation: "member".into(),
            subject_type: "role".into(),
            subject_id: parent_role_name.into(),
            subject_relation: "member".into(),
        };
        if enabled {
            // Authorization may grant only durable, approved inheritance. The
            // session lock still excludes removals after this commit.
            tx.commit().await?;
            if let Some(access) = access {
                access.set(tenant_id, &tuple, true).await?;
            }
        } else {
            if let Some(access) = access {
                access.set(tenant_id, &tuple, false).await?;
            }
            tx.commit().await?;
        }
        connection.release_now().await?;
        Ok(())
    }
}

#[async_trait::async_trait]
impl IamStore for PgIamStore {
    async fn search_users(
        &self,
        tenant_id: &str,
        user_id: &str,
        administrator: bool,
        search: &str,
        limit: usize,
    ) -> Result<Vec<IamUser>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(r#"
            WITH RECURSIVE visible(role_name) AS (
                SELECT role_name FROM iam_user_roles WHERE tenant_id=$1 AND user_id=$2
                UNION
                SELECT edge.role_name FROM iam_role_parents edge JOIN visible ON edge.parent_role_name=visible.role_name WHERE edge.tenant_id=$1
            )
            SELECT user_id, display_name, email, is_active, created_at, updated_at
            FROM iam_users u WHERE tenant_id=$1 AND deleted_at IS NULL
              AND ($3 OR user_id=$2 OR (EXISTS (
                SELECT 1 FROM iam_user_roles a JOIN visible USING(role_name)
                WHERE a.tenant_id=$1 AND a.user_id=u.user_id
              ) AND NOT EXISTS (
                SELECT 1 FROM iam_user_roles a
                WHERE a.tenant_id=$1 AND a.user_id=u.user_id
                  AND a.role_name NOT IN (SELECT role_name FROM visible)
              )))
              AND (user_id || ' ' || COALESCE(display_name,'') || ' ' || COALESCE(email,'')) ILIKE '%' || $4 || '%' ESCAPE '\'
            ORDER BY user_id LIMIT $5
        "#).bind(tenant_id).bind(user_id).bind(administrator).bind(escape_search(search))
            .bind(i64::try_from(limit.clamp(1,HARD_LIMIT))?).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        rows.into_iter()
            .map(|row| {
                Ok(IamUser {
                    user_id: row.try_get("user_id")?,
                    display_name: row.try_get("display_name")?,
                    email: row.try_get("email")?,
                    is_active: row.try_get("is_active")?,
                    created_at_ms: Self::to_ms(row.try_get("created_at")?),
                    updated_at_ms: Self::to_ms(row.try_get("updated_at")?),
                })
            })
            .collect()
    }

    async fn search_roles(
        &self,
        tenant_id: &str,
        user_id: &str,
        administrator: bool,
        search: &str,
        limit: usize,
    ) -> Result<Vec<IamRole>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows=sqlx::query(r#"
            WITH RECURSIVE visible(role_name) AS (
                SELECT role_name FROM iam_user_roles WHERE tenant_id=$1 AND user_id=$2
                UNION
                SELECT edge.role_name FROM iam_role_parents edge JOIN visible ON edge.parent_role_name=visible.role_name WHERE edge.tenant_id=$1
            )
            SELECT role_name,created_at,updated_at FROM iam_roles
            WHERE tenant_id=$1 AND deleted_at IS NULL AND ($3 OR role_name IN (SELECT role_name FROM visible))
              AND role_name ILIKE '%' || $4 || '%' ESCAPE '\'
            ORDER BY role_name LIMIT $5
        "#).bind(tenant_id).bind(user_id).bind(administrator).bind(escape_search(search))
            .bind(i64::try_from(limit.clamp(1,HARD_LIMIT))?).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        rows.into_iter()
            .map(|row| {
                Ok(IamRole {
                    role_name: row.try_get("role_name")?,
                    created_at_ms: Self::to_ms(row.try_get("created_at")?),
                    updated_at_ms: Self::to_ms(row.try_get("updated_at")?),
                })
            })
            .collect()
    }

    async fn effective_role_names_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let roles=sqlx::query_scalar(r#"
            WITH RECURSIVE visible(role_name) AS (
                SELECT assignment.role_name FROM iam_user_roles assignment JOIN iam_users u USING(tenant_id,user_id)
                WHERE assignment.tenant_id=$1 AND assignment.user_id=$2 AND u.is_active AND u.deleted_at IS NULL
                UNION
                SELECT edge.role_name FROM iam_role_parents edge JOIN visible ON edge.parent_role_name=visible.role_name WHERE edge.tenant_id=$1
            )
            SELECT r.role_name FROM iam_roles r JOIN visible USING(role_name) WHERE r.tenant_id=$1 AND r.deleted_at IS NULL ORDER BY r.role_name
        "#).bind(tenant_id).bind(user_id).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        Ok(roles)
    }

    async fn set_role_parent(
        &self,
        tenant_id: &str,
        role_name: &str,
        parent_role_name: &str,
        enabled: bool,
    ) -> Result<()> {
        self.update_role_parent(
            tenant_id,
            role_name,
            parent_role_name,
            enabled,
            None,
        )
        .await
    }

    async fn set_role_parent_with_access(
        &self,
        tenant_id: &str,
        role_name: &str,
        parent_role_name: &str,
        enabled: bool,
        access: &dyn crate::application::ports::access_store::AccessStore,
    ) -> Result<()> {
        self.update_role_parent(
            tenant_id,
            role_name,
            parent_role_name,
            enabled,
            Some(access),
        )
        .await
    }

    async fn cedar_policies(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<CedarPolicy>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows=sqlx::query("SELECT id,content,is_active FROM auth_policies WHERE tenant_id=$1 ORDER BY id LIMIT 129")
            .bind(tenant_id).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        anyhow::ensure!(
            rows.len() <= TENANT_POLICY_LIMIT,
            "This organization has exceeded its limit of 128 access policies"
        );
        rows.into_iter()
            .map(|row| {
                Ok(CedarPolicy {
                    id: row.try_get("id")?,
                    content: row.try_get("content")?,
                    active: row.try_get("is_active")?,
                })
            })
            .collect()
    }

    async fn verify_role_unassignment(
        &self,
        tenant_id: &str,
        user_id: &str,
        role_name: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let imported:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM iam_federated_role_assignments WHERE tenant_id=$1 AND user_id=$2 AND role_name=$3)")
            .bind(tenant_id).bind(user_id).bind(role_name).fetch_one(&mut *tx).await?;
        tx.commit().await?;
        anyhow::ensure!(
            !imported,
            "This role is managed by your identity provider; change its group mapping first"
        );
        Ok(())
    }

    /// Creates or reconfigures a live tenant user without reviving tombstones.
    #[cfg(test)]
    async fn create_user(
        &self,
        tenant_id: &str,
        user_id: &str,
        is_active: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let result = sqlx::query(
            r#"
            INSERT INTO iam_users (tenant_id, user_id, is_active)
            VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, user_id) DO UPDATE
            SET is_active = EXCLUDED.is_active, updated_at = NOW()
            WHERE iam_users.deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .bind(is_active)
        .execute(&mut *tx)
        .await
        .context("Failed to upsert iam_users")?;
        if result.rows_affected() != 1 {
            bail!("User identifier is tombstoned and cannot be reused");
        }
        tx.commit().await.context("Failed to commit create_user")?;
        Ok(())
    }

    /// Keeps an authorized caller active even when administrators race each
    /// other.
    async fn set_user_guarded(
        &self,
        tenant_id: &str,
        actor_id: &str,
        user_id: &str,
        is_active: bool,
        upsert: bool,
    ) -> Result<()> {
        if !is_active && actor_id == user_id {
            bail!("You cannot suspend or delete your own account");
        }
        let mut tx = self.database.tenant(tenant_id).await?;
        // Manage permission requires tenant#administrator. Serializing state
        // changes and rechecking its active caller preserves at least
        // one administrator without duplicating SpiceDB's
        // administrator relationships in Postgres.
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 713058))",
        )
        .bind(tenant_id)
        .execute(&mut *tx)
        .await?;
        let active: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM iam_users WHERE tenant_id = $1 AND user_id = $2 AND is_active AND deleted_at IS NULL)"
        ).bind(tenant_id).bind(actor_id).fetch_one(&mut *tx).await?;
        if !active {
            bail!("An active administrator must perform this operation");
        }
        let statement = if upsert {
            "INSERT INTO iam_users (tenant_id, user_id, is_active) VALUES ($1, $2, $3) ON CONFLICT (tenant_id, user_id) DO UPDATE SET is_active = EXCLUDED.is_active, updated_at = NOW() WHERE iam_users.deleted_at IS NULL"
        } else {
            "UPDATE iam_users SET is_active = $3, updated_at = NOW() WHERE tenant_id = $1 AND user_id = $2 AND deleted_at IS NULL"
        };
        let result = sqlx::query(statement)
            .bind(tenant_id)
            .bind(user_id)
            .bind(is_active)
            .execute(&mut *tx)
            .await?;
        if result.rows_affected() != 1 {
            bail!("User is unavailable or its identifier cannot be reused");
        }
        tx.commit()
            .await
            .context("Failed to commit guarded user mutation")
    }

    /// Tombstones a tenant user and removes durable role assignments.
    async fn delete_user(&self, tenant_id: &str, user_id: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant_id).await?;
        sqlx::query(
            "DELETE FROM iam_user_roles WHERE tenant_id = $1 AND user_id = $2",
        )
        .bind(tenant_id)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .context("Failed to remove deleted user assignments")?;
        let result = sqlx::query(
            r#"
            UPDATE iam_users
            SET is_active = FALSE, deleted_at = NOW(), updated_at = NOW()
            WHERE tenant_id = $1 AND user_id = $2 AND deleted_at IS NULL
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .context("Failed to soft-delete iam_user")?;
        if result.rows_affected() != 1 {
            bail!("User is unavailable");
        }
        tx.commit().await.context("Failed to commit delete_user")
    }

    /// Keeps manual and automatically approved roles under one atomic quota.
    async fn create_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<()> {
        self.create_roles(tenant_id, &[role_name]).await
    }

    async fn create_roles(
        &self,
        tenant_id: &str,
        role_names: &[&str],
    ) -> Result<()> {
        anyhow::ensure!(
            role_names.len() <= 64,
            "At most 64 role names can be created at once"
        );
        for name in role_names {
            crate::domain::sso::validate_identifier(name)?;
        }
        let mut tx = self.database.tenant(tenant_id).await?;
        // Archived names count too, so create/delete loops cannot evade
        // storage bounds. The lock also makes concurrent manual and
        // import batches safe.
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,713059))",
        )
        .bind(tenant_id)
        .execute(&mut *tx)
        .await?;
        let within_quota:bool=sqlx::query_scalar("SELECT COUNT(*) + (SELECT COUNT(DISTINCT name) FROM unnest($2::text[]) name WHERE NOT EXISTS(SELECT 1 FROM iam_roles existing WHERE existing.tenant_id=$1 AND existing.role_name=name)) <= $3 FROM iam_roles WHERE tenant_id=$1")
            .bind(tenant_id).bind(role_names).bind(TENANT_ROLE_LIMIT).fetch_one(&mut *tx).await?;
        anyhow::ensure!(
            within_quota,
            "This organization has reached its limit of 256 role names, including archived roles"
        );
        let tombstoned:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM iam_roles WHERE tenant_id=$1 AND role_name=ANY($2) AND deleted_at IS NOT NULL)")
            .bind(tenant_id).bind(role_names).fetch_one(&mut *tx).await?;
        anyhow::ensure!(
            !tombstoned,
            "Role identifier is tombstoned and cannot be reused"
        );
        sqlx::query("INSERT INTO iam_roles(tenant_id,role_name) SELECT $1,name FROM unnest($2::text[]) name GROUP BY name ON CONFLICT(tenant_id,role_name) DO UPDATE SET updated_at=NOW() WHERE iam_roles.deleted_at IS NULL")
            .bind(tenant_id).bind(role_names).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    async fn verify_role_deletion(
        &self,
        tenant: &str,
        role: &str,
    ) -> Result<()> {
        let mut transaction = self.database.tenant(tenant).await?;
        Self::role_deletion_users(&mut transaction, tenant, role).await?;
        transaction.commit().await?;
        Ok(())
    }

    async fn delete_role(&self, tenant: &str, role: &str) -> Result<()> {
        self.delete_role_state(tenant, role, None).await
    }

    async fn delete_role_with_access(
        &self,
        tenant: &str,
        role: &str,
        access: &dyn AccessStore,
    ) -> Result<()> {
        self.delete_role_state(tenant, role, Some(access)).await
    }

    async fn set_role_membership_with_access(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
        enabled: bool,
        access: &dyn AccessStore,
    ) -> Result<()> {
        self.update_role_membership(tenant, user, role, enabled, Some(access))
            .await
    }

    async fn assign_role_to_user(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
    ) -> Result<()> {
        self.update_role_membership(tenant, user, role, true, None)
            .await
    }

    async fn unassign_role_from_user(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
    ) -> Result<()> {
        self.update_role_membership(tenant, user, role, false, None)
            .await
    }

    /// Lists current tenant users with a hard result bound.
    async fn list_users(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<IamUser>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT user_id, display_name, email, is_active, created_at, updated_at
            FROM iam_users
            WHERE tenant_id = $1 AND deleted_at IS NULL
            ORDER BY user_id
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list iam_users")?;
        tx.commit()
            .await
            .context("Failed to commit iam_users read")?;

        let mut users = Vec::with_capacity(rows.len());
        for row in rows {
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            users.push(IamUser {
                user_id: row.try_get("user_id")?,
                display_name: row.try_get("display_name")?,
                email: row.try_get("email")?,
                is_active: row.try_get("is_active")?,
                created_at_ms: Self::to_ms(created_at),
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(users)
    }

    /// Lists current tenant roles with a hard result bound.
    async fn list_roles(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<IamRole>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT role_name, created_at, updated_at
            FROM iam_roles
            WHERE tenant_id = $1 AND deleted_at IS NULL
            ORDER BY role_name
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list iam_roles")?;
        tx.commit()
            .await
            .context("Failed to commit iam_roles read")?;

        let mut roles = Vec::with_capacity(rows.len());
        for row in rows {
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            roles.push(IamRole {
                role_name: row.try_get("role_name")?,
                created_at_ms: Self::to_ms(created_at),
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(roles)
    }

    /// Lists assignments whose user and role remain live.
    async fn list_role_assignments(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<RoleAssignment>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT assignment.user_id, assignment.role_name,
                   assignment.created_at
            FROM iam_user_roles AS assignment
            JOIN iam_users AS tenant_user
              ON tenant_user.tenant_id = assignment.tenant_id
             AND tenant_user.user_id = assignment.user_id
             AND tenant_user.deleted_at IS NULL
            JOIN iam_roles AS tenant_role
              ON tenant_role.tenant_id = assignment.tenant_id
             AND tenant_role.role_name = assignment.role_name
             AND tenant_role.deleted_at IS NULL
            WHERE assignment.tenant_id = $1
            ORDER BY user_id, role_name
            LIMIT $2
            "#,
        )
        .bind(tenant_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list iam_user_roles")?;
        tx.commit()
            .await
            .context("Failed to commit iam_user_roles read")?;

        let mut assignments = Vec::with_capacity(rows.len());
        for row in rows {
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            assignments.push(RoleAssignment {
                user_id: row.try_get("user_id")?,
                role_name: row.try_get("role_name")?,
                created_at_ms: Self::to_ms(created_at),
            });
        }
        Ok(assignments)
    }

    /// Lists current role names for authorization cleanup.
    async fn role_names_for_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<Vec<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let roles = sqlx::query_scalar(
            r#"
            SELECT assignment.role_name
            FROM iam_user_roles AS assignment
            JOIN iam_roles AS role
              ON role.tenant_id = assignment.tenant_id
             AND role.role_name = assignment.role_name
             AND role.deleted_at IS NULL
            WHERE assignment.tenant_id = $1 AND assignment.user_id = $2
            ORDER BY assignment.role_name
            "#,
        )
        .bind(tenant_id)
        .bind(user_id)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list user roles for cleanup")?;
        tx.commit().await?;
        Ok(roles)
    }

    /// Lists current role members for authorization cleanup.
    async fn user_ids_for_role(
        &self,
        tenant_id: &str,
        role_name: &str,
    ) -> Result<Vec<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let users = sqlx::query_scalar(
            r#"
            SELECT assignment.user_id
            FROM iam_user_roles AS assignment
            JOIN iam_users AS tenant_user
              ON tenant_user.tenant_id = assignment.tenant_id
             AND tenant_user.user_id = assignment.user_id
             AND tenant_user.deleted_at IS NULL
            WHERE assignment.tenant_id = $1 AND assignment.role_name = $2
            ORDER BY assignment.user_id
            "#,
        )
        .bind(tenant_id)
        .bind(role_name)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list role users for cleanup")?;
        tx.commit().await?;
        Ok(users)
    }

    /// Concatenates active tenant policies in deterministic identifier order.
    async fn get_active_cedar_policies(
        &self,
        tenant_id: &str,
    ) -> Result<Option<String>> {
        let mut tx = self.database.tenant(tenant_id).await?;
        let rows = sqlx::query(
            r#"
            SELECT content
            FROM auth_policies
            WHERE tenant_id = $1 AND is_active = TRUE
            ORDER BY id
            LIMIT 129
            "#,
        )
        .bind(tenant_id)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to load active Cedar policies")?;
        tx.commit()
            .await
            .context("Failed to commit Cedar policy read")?;
        if rows.is_empty() {
            return Ok(None);
        }
        anyhow::ensure!(
            rows.len() <= TENANT_POLICY_LIMIT,
            "This organization has exceeded its limit of 128 access policies"
        );
        let mut combined =
            String::with_capacity(rows.len() * POLICY_CONTENT_LIMIT);
        for row in rows {
            let content: String = row.try_get("content")?;
            anyhow::ensure!(
                content.len() <= POLICY_CONTENT_LIMIT &&
                    combined
                        .len()
                        .saturating_add(content.len())
                        .saturating_add(1) <=
                        TENANT_POLICY_CONTENT_LIMIT,
                "This organization has exceeded its access policy content budget"
            );
            combined.push_str(&content);
            combined.push('\n');
        }
        Ok(Some(combined))
    }

    /// Validates persistence scope and upserts one Cedar policy document.
    async fn upsert_cedar_policy(
        &self,
        tenant_id: &str,
        policy_id: &str,
        content: &str,
        is_active: bool,
    ) -> Result<()> {
        crate::domain::sso::validate_identifier(policy_id)?;
        anyhow::ensure!(
            content.len() <= POLICY_CONTENT_LIMIT,
            "Access policies must contain at most 8192 bytes"
        );
        let mut tx = self.database.tenant(tenant_id).await?;
        // Inactive policies retain their names, so toggling status cannot
        // evade the quota or make authorization parse unbounded
        // tenant-controlled data.
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,713059))",
        )
        .bind(tenant_id)
        .execute(&mut *tx)
        .await?;
        let within_quota:bool=sqlx::query_scalar("SELECT COUNT(*) < 128 OR EXISTS(SELECT 1 FROM auth_policies WHERE tenant_id=$1 AND id=$2) FROM auth_policies WHERE tenant_id=$1")
            .bind(tenant_id).bind(policy_id).fetch_one(&mut *tx).await?;
        anyhow::ensure!(
            within_quota,
            "This organization has reached its limit of 128 access policies, including inactive policies"
        );
        sqlx::query(
            r#"
            INSERT INTO auth_policies (tenant_id, id, content, is_active)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, id) DO UPDATE
            SET content = EXCLUDED.content,
                is_active = EXCLUDED.is_active,
                updated_at = NOW()
            "#,
        )
        .bind(tenant_id)
        .bind(policy_id)
        .bind(content)
        .bind(is_active)
        .execute(&mut *tx)
        .await
        .context("Failed to upsert Cedar policy")?;
        tx.commit()
            .await
            .context("Failed to commit Cedar policy update")?;
        Ok(())
    }
}

/// Escapes LIKE metacharacters because directory search treats input
/// literally.
fn escape_search(search: &str) -> String {
    search
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

#[cfg(test)]
mod administration_tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;

    use super::*;
    use crate::application::ports::access_store::{AccessStore, AccessTuple};

    struct Projection {
        database: Database,
        fail: bool,
        changes: AtomicUsize,
    }

    #[async_trait::async_trait]
    impl AccessStore for Projection {
        async fn tuples(&self, _: &str, _: usize) -> Result<Vec<AccessTuple>> {
            Ok(Vec::new())
        }

        async fn set(
            &self,
            tenant: &str,
            _: &AccessTuple,
            _: bool,
        ) -> Result<()> {
            let mut transaction = self.database.tenant(tenant).await?;
            let unlocked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(hashtextextended($1,713059))")
                .bind(tenant).fetch_one(&mut *transaction).await?;
            anyhow::ensure!(
                !unlocked,
                "Role projection requires the lifecycle lock"
            );
            transaction.commit().await?;
            anyhow::ensure!(!self.fail, "Authorization fixture unavailable");
            self.changes.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }

    #[tokio::test]
    async fn directory_hierarchy_is_searchable_bounded_and_tenant_isolated()
    -> Result<()> {
        let container = Postgres::default()
            .with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;".to_vec()).with_tag("18.6-alpine").start().await?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{host}:{port}/postgres"
        ))
        .await?;
        let store = PgIamStore::new(database.clone());
        for tenant in ["tenant_a", "tenant_b"] {
            for user in ["supervisor", "analyst", "outsider"] {
                store.create_user(tenant, user, true).await?;
            }
            for role in ["senior", "junior", "unrelated"] {
                store.create_role(tenant, role).await?;
            }
            store
                .assign_role_to_user(tenant, "supervisor", "senior")
                .await?;
            store
                .assign_role_to_user(tenant, "analyst", "junior")
                .await?;
            store
                .assign_role_to_user(tenant, "outsider", "unrelated")
                .await?;
        }
        store
            .set_role_parent("tenant_a", "junior", "senior", true)
            .await?;
        anyhow::ensure!(
            store
                .set_role_parent("tenant_a", "senior", "junior", true)
                .await
                .is_err()
        );
        anyhow::ensure!(
            store
                .set_role_parent("tenant_a", "junior", "unrelated", true)
                .await
                .is_err(),
            "Replacing a parent must require explicit removal of its access relationship"
        );
        let roles = store
            .search_roles("tenant_a", "supervisor", false, "", 100)
            .await?;
        anyhow::ensure!(
            roles
                .iter()
                .map(|r| r.role_name.as_str())
                .collect::<Vec<_>>() ==
                ["junior", "senior"]
        );
        let users = store
            .search_users("tenant_a", "supervisor", false, "anal", 100)
            .await?;
        anyhow::ensure!(
            users.len() == 1 &&
                users.first().is_some_and(|u| u.user_id == "analyst")
        );
        anyhow::ensure!(
            store
                .search_users("tenant_a", "supervisor", false, "outsider", 100)
                .await?
                .is_empty()
        );
        anyhow::ensure!(
            store
                .search_users("tenant_a", "supervisor", true, "outsider", 1)
                .await?
                .len() ==
                1
        );
        anyhow::ensure!(
            store
                .search_roles("tenant_b", "supervisor", false, "junior", 100)
                .await?
                .is_empty()
        );
        anyhow::ensure!(
            store
                .search_users("tenant_a", "supervisor", true, "%", 100)
                .await?
                .is_empty()
        );
        store
            .assign_role_to_user("tenant_a", "analyst", "unrelated")
            .await?;
        anyhow::ensure!(
            store
                .search_users("tenant_a", "supervisor", false, "analyst", 100)
                .await?
                .is_empty(),
            "A lower role must not reveal a principal with unrelated privileges"
        );
        let mut tx = database.tenant("tenant_b").await?;
        sqlx::query("INSERT INTO iam_roles(tenant_id,role_name) SELECT 'tenant_b','bounded_' || value::text FROM generate_series(1,252) value").execute(&mut *tx).await?;
        tx.commit().await?;
        anyhow::ensure!(
            store
                .create_roles("tenant_b", &["batch_one", "batch_two"])
                .await
                .is_err(),
            "An oversized mapping batch must fail atomically"
        );
        anyhow::ensure!(
            store
                .search_roles("tenant_b", "supervisor", true, "batch_", 100)
                .await?
                .is_empty(),
            "A failed quota batch must create no partial roles"
        );
        store.create_role("tenant_b", "last_available").await?;
        anyhow::ensure!(
            store
                .create_role("tenant_b", "quota_exceeded")
                .await
                .is_err(),
            "Role names must have a tenant quota"
        );
        store.create_role("tenant_b", "senior").await?;
        let mut tx = database.tenant("tenant_b").await?;
        sqlx::query("INSERT INTO sso_role_sync_configs(tenant_id,connection_id,configuration) VALUES('tenant_b','connection','{\"mappings\":[{\"roleName\":\"last_available\",\"externalGroup\":\"team\"}],\"enabled\":false}')").execute(&mut *tx).await?;
        tx.commit().await?;
        anyhow::ensure!(
            store
                .delete_role("tenant_b", "last_available")
                .await
                .is_err(),
            "A configured provider mapping must prevent deletion even while synchronization is disabled"
        );
        let mut parent = String::from("senior");
        for level in 0..17 {
            let role = format!("level_{level}");
            store.create_role("tenant_a", &role).await?;
            let result = store
                .set_role_parent("tenant_a", &role, &parent, true)
                .await;
            if level < 16 {
                result?;
            } else {
                anyhow::ensure!(
                    result.is_err(),
                    "A bounded hierarchy must prevent expensive authorization recursion"
                );
            }
            parent = role;
        }
        anyhow::ensure!(
            store
                .upsert_cedar_policy(
                    "tenant_a",
                    "oversized",
                    &" ".repeat(8193),
                    false
                )
                .await
                .is_err(),
            "Inactive policies must respect the same content budget"
        );
        let mut tx = database.tenant("tenant_a").await?;
        sqlx::query("INSERT INTO auth_policies(tenant_id,id,content,is_active) SELECT 'tenant_a','policy_' || value::text,'permit(principal,action,resource);',FALSE FROM generate_series(1,128) value").execute(&mut *tx).await?;
        tx.commit().await?;
        anyhow::ensure!(
            store
                .upsert_cedar_policy(
                    "tenant_a",
                    "policy_overflow",
                    "permit(principal,action,resource);",
                    false
                )
                .await
                .is_err(),
            "Inactive policies count toward the tenant quota"
        );
        store
            .upsert_cedar_policy(
                "tenant_a",
                "policy_1",
                "permit(principal,action,resource);",
                true,
            )
            .await?;
        anyhow::ensure!(
            store.get_active_cedar_policies("tenant_a").await?.is_some()
        );
        for role in ["commit_parent", "commit_member"] {
            store.create_role("tenant_a", role).await?;
        }
        let access = Projection {
            database: database.clone(),
            fail: false,
            changes: AtomicUsize::new(0),
        };
        let mut tx = database.tenant("tenant_a").await?;
        sqlx::query("CREATE FUNCTION reject_iam_commit_for_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.role_name IN ('commit_parent','commit_member') THEN RAISE EXCEPTION 'Injected deferred commit failure'; END IF; RETURN NEW; END $$").execute(&mut *tx).await?;
        for statement in [
            "CREATE CONSTRAINT TRIGGER reject_commit AFTER INSERT OR UPDATE ON iam_role_parents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_iam_commit_for_test()",
            "CREATE CONSTRAINT TRIGGER reject_commit AFTER INSERT OR UPDATE ON iam_user_roles DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_iam_commit_for_test()",
        ] {
            sqlx::query(statement).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        anyhow::ensure!(
            store
                .set_role_parent_with_access(
                    "tenant_a",
                    "commit_parent",
                    "senior",
                    true,
                    &access
                )
                .await
                .is_err()
        );
        anyhow::ensure!(
            access.changes.load(Ordering::Relaxed) == 0,
            "A failed deferred hierarchy commit must not publish an external inheritance grant"
        );
        anyhow::ensure!(
            store
                .set_role_membership_with_access(
                    "tenant_a",
                    "outsider",
                    "commit_member",
                    true,
                    &access
                )
                .await
                .is_err()
        );
        anyhow::ensure!(
            access.changes.load(Ordering::Relaxed) == 0,
            "A failed deferred membership commit must not publish or compensate any external grant"
        );
        store.create_role("tenant_a", "lifecycle").await?;
        let failing = Projection {
            database: database.clone(),
            fail: true,
            changes: AtomicUsize::new(0),
        };
        anyhow::ensure!(
            store
                .set_role_membership_with_access(
                    "tenant_a",
                    "outsider",
                    "lifecycle",
                    true,
                    &failing
                )
                .await
                .is_err()
        );
        anyhow::ensure!(
            store
                .role_names_for_user("tenant_a", "outsider")
                .await?
                .iter()
                .any(|role| role == "lifecycle"),
            "A durable approved assignment must remain available for projection retry"
        );
        let access = Projection {
            database: database.clone(),
            fail: false,
            changes: AtomicUsize::new(0),
        };
        store
            .set_role_membership_with_access(
                "tenant_a",
                "outsider",
                "lifecycle",
                true,
                &access,
            )
            .await?;
        anyhow::ensure!(
            store
                .role_names_for_user("tenant_a", "outsider")
                .await?
                .iter()
                .any(|role| role == "lifecycle")
        );
        store
            .set_role_membership_with_access(
                "tenant_a",
                "outsider",
                "lifecycle",
                false,
                &access,
            )
            .await?;
        let before = access.changes.load(Ordering::Relaxed);
        anyhow::ensure!(
            store
                .delete_role_with_access("tenant_b", "last_available", &access)
                .await
                .is_err()
        );
        anyhow::ensure!(
            access.changes.load(Ordering::Relaxed) == before,
            "Provider mapping rejection must precede external revocation"
        );
        store
            .delete_role_with_access("tenant_a", "lifecycle", &access)
            .await?;
        anyhow::ensure!(
            store
                .search_roles("tenant_a", "supervisor", true, "lifecycle", 100)
                .await?
                .is_empty()
        );
        Ok(())
    }
}
