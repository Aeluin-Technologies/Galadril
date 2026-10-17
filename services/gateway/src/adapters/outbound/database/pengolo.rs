//! PostgreSQL workspace records are separated from expiring, unlogged
//! sessions.

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use sqlx::{AssertSqlSafe, Postgres, Transaction};

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::access_store::{AccessStore, AccessTuple};
use crate::application::ports::conversation_store::EvidenceSource;
use crate::application::ports::pengolo_store::{
    Collection, ObjectExpiry, PengoloObjects, PengoloStore, RecordQuery,
    Reservation, StagedFile,
};
use crate::domain::pengolo::{
    OutputEvent, Record, SendInput, TEMPORARY_TTL_MS,
};

pub struct PgPengoloStore {
    database: Database,
}

// Table selectors are closed constants; every caller-controlled value is
// bound.
fn table(temporary: bool) -> &'static str {
    if temporary {
        "pengolo_ephemeral_records"
    } else {
        "pengolo_records"
    }
}
fn output_table(temporary: bool) -> &'static str {
    if temporary {
        "pengolo_ephemeral_output"
    } else {
        "pengolo_output"
    }
}
fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

impl PgPengoloStore {
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    async fn read(
        tx: &mut Transaction<'static, Postgres>,
        collection: Collection,
        id: &str,
        temporary: bool,
        lock: bool,
    ) -> Result<Option<Record>> {
        let query = format!(
            "SELECT jsonb_build_object('id',id,'ownerId',owner_id,'projectId',project_id,'expiresAt',expires_at,'revision',revision,'updatedAt',updated_at,'data',data) FROM {} WHERE kind=$1 AND id=$2 AND (expires_at IS NULL OR expires_at>$3) {}",
            table(temporary),
            if lock { "FOR UPDATE" } else { "" }
        );
        let value: Option<Value> =
            sqlx::query_scalar(AssertSqlSafe(query.as_str()))
                .bind(collection.as_str())
                .bind(id)
                .bind(now())
                .fetch_optional(&mut **tx)
                .await?;
        value
            .map(serde_json::from_value)
            .transpose()
            .context("Invalid workspace record")
    }

    async fn write(
        tx: &mut Transaction<'static, Postgres>,
        tenant: &str,
        collection: Collection,
        record: &Record,
        temporary: bool,
        expected: Option<i64>,
    ) -> Result<i64> {
        let updated_at = now();
        let result = if let Some(revision) = expected {
            sqlx::query(AssertSqlSafe(format!("UPDATE {} SET owner_id=$1,project_id=$2,expires_at=$3,data=$4,revision=revision+1,updated_at=$5 WHERE tenant_id=$6 AND kind=$7 AND id=$8 AND revision=$9", table(temporary))))
                .bind(&record.owner_id).bind(&record.project_id).bind(record.expires_at).bind(&record.data).bind(updated_at).bind(tenant).bind(collection.as_str()).bind(&record.id).bind(revision).execute(&mut **tx).await?
        } else {
            sqlx::query(AssertSqlSafe(format!("INSERT INTO {} (tenant_id,kind,id,owner_id,project_id,expires_at,revision,updated_at,data) VALUES ($1,$2,$3,$4,$5,$6,1,$7,$8)", table(temporary))))
                .bind(tenant).bind(collection.as_str()).bind(&record.id).bind(&record.owner_id).bind(&record.project_id).bind(record.expires_at).bind(updated_at).bind(&record.data).execute(&mut **tx).await?
        };
        ensure!(result.rows_affected() == 1, "Workspace revision conflict");
        if let Some(deadline) = record.expires_at {
            sqlx::query("INSERT INTO pengolo_retention(tenant_id,kind,id,temporary,expires_at,object_key,staging_key) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(tenant_id,kind,id,temporary) DO UPDATE SET expires_at=EXCLUDED.expires_at,object_key=EXCLUDED.object_key,staging_key=EXCLUDED.staging_key")
                .bind(tenant).bind(collection.as_str()).bind(&record.id).bind(temporary).bind(deadline).bind(if matches!(collection,Collection::File) {record.data.get("key").and_then(Value::as_str)} else {None}).bind(if matches!(collection,Collection::File) {record.data.get("stagingKey").and_then(Value::as_str)} else {None}).execute(&mut **tx).await?;
        }
        Ok(updated_at)
    }
}

#[async_trait::async_trait]
impl PengoloStore for PgPengoloStore {
    async fn promote_file(
        &self,
        tenant: &str,
        record: &Record,
        temporary: bool,
        file: &StagedFile<'_>,
        objects: &dyn PengoloObjects,
    ) -> Result<Record> {
        let mut tx = self.database.tenant(tenant).await?;
        let mut current =
            Self::read(&mut tx, Collection::File, &record.id, temporary, true)
                .await?
                .context("File unavailable")?;
        ensure!(
            current.expires_at.is_some_and(|deadline| deadline > now()),
            "Upload reservation expired"
        );
        ensure!(
            current.owner_id == record.owner_id &&
                current.project_id == record.project_id,
            "File scope changed"
        );
        if current.data.get("status").and_then(Value::as_str) == Some("ready")
        {
            tx.commit().await?;
            return Ok(current);
        }
        ensure!(
            current.revision == record.revision &&
                current.data.get("status").and_then(Value::as_str) ==
                    Some("uploading"),
            "File revision conflict"
        );
        let uploaded_at = objects.finalize(file).await?;
        ensure!(
            current.expires_at.is_some_and(|deadline| deadline > now()),
            "Upload reservation expired"
        );
        current.expires_at = Some(
            uploaded_at +
                if temporary {
                    TEMPORARY_TTL_MS
                } else {
                    crate::domain::pengolo::NORMAL_TTL_MS
                },
        );
        current
            .data
            .as_object_mut()
            .context("Invalid file metadata")?
            .insert("status".into(), json!("ready"));
        current.updated_at = Self::write(
            &mut tx,
            tenant,
            Collection::File,
            &current,
            temporary,
            Some(record.revision),
        )
        .await?;
        current.revision += 1;
        tx.commit().await?;
        Ok(current)
    }

    async fn purge_file(
        &self,
        tenant: &str,
        id: &str,
        temporary: bool,
        staging: bool,
        objects: &dyn PengoloObjects,
    ) -> Result<bool> {
        let mut tx = self.database.tenant(tenant).await?;
        // Lock the file before its retention row, matching promotion's lock
        // order. An expired UNLOGGED record may be absent after a
        // PostgreSQL crash.
        sqlx::query(AssertSqlSafe(format!("SELECT id FROM {} WHERE tenant_id=$1 AND kind='file' AND id=$2 FOR UPDATE", table(temporary))))
            .bind(tenant).bind(id).fetch_optional(&mut *tx).await?;
        let kind = if staging { "staging" } else { "file" };
        let pending: Option<(i64, Option<String>, Option<String>)> = sqlx::query_as("SELECT expires_at,object_key,staging_key FROM pengolo_retention WHERE tenant_id=$1 AND kind=$2 AND id=$3 AND temporary=$4 FOR UPDATE")
            .bind(tenant).bind(kind).bind(id).bind(temporary).fetch_optional(&mut *tx).await?;
        let Some((deadline, key, source)) = pending else {
            return Ok(false);
        };
        if deadline > now() {
            return Ok(false);
        }
        if !staging && let Some(key) = key {
            objects.delete(&key).await?;
        }
        if let Some(source) = source {
            objects.delete_staging(&source).await?;
        }
        if staging {
            sqlx::query(AssertSqlSafe(format!("UPDATE {} SET data=data-'stagingKey'-'stagingExpiresAt',revision=revision+1 WHERE tenant_id=$1 AND kind='file' AND id=$2", table(temporary))))
                .bind(tenant).bind(id).execute(&mut *tx).await?;
            sqlx::query("UPDATE pengolo_retention SET staging_key=NULL WHERE tenant_id=$1 AND kind='file' AND id=$2 AND temporary=$3")
                .bind(tenant).bind(id).bind(temporary).execute(&mut *tx).await?;
        } else {
            sqlx::query(AssertSqlSafe(format!(
                "DELETE FROM {} WHERE tenant_id=$1 AND kind='file' AND id=$2",
                table(temporary)
            )))
            .bind(tenant)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        }
        sqlx::query("DELETE FROM pengolo_retention WHERE tenant_id=$1 AND kind=$2 AND id=$3 AND temporary=$4")
            .bind(tenant).bind(kind).bind(id).bind(temporary).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(true)
    }

    async fn reserve_file(
        &self,
        tenant: &str,
        record: &Record,
        temporary: bool,
    ) -> Result<Record> {
        let mut tx = self.database.tenant(tenant).await?;
        let session =
            record.data.get("temporarySession").and_then(Value::as_str);
        let scope = record.project_id.as_deref().unwrap_or(&record.owner_id);
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,59032))",
        )
        .bind(format!(
            "{tenant}/{temporary}/{scope}/{}",
            session.unwrap_or("")
        ))
        .execute(&mut *tx)
        .await?;
        let names:Vec<String>=sqlx::query_scalar(AssertSqlSafe(format!("SELECT data->>'name' FROM {} WHERE tenant_id=$1 AND kind='file' AND project_id IS NOT DISTINCT FROM $2 AND ($2::text IS NOT NULL OR owner_id=$3) AND (data->>'temporarySession') IS NOT DISTINCT FROM $4 AND expires_at>$5 LIMIT 10000",table(temporary)))).bind(tenant).bind(&record.project_id).bind(&record.owner_id).bind(session).bind(now()).fetch_all(&mut *tx).await?;
        ensure!(names.len() < 10000, "Library file limit reached");
        let occupied =
            names.into_iter().map(|name| name.to_lowercase()).collect();
        let original = record
            .data
            .get("name")
            .and_then(Value::as_str)
            .context("Missing file name")?;
        let name =
            crate::domain::pengolo::available_file_name(original, &occupied)?;
        let mut saved = record.clone();
        saved
            .data
            .as_object_mut()
            .context("Invalid file metadata")?
            .insert("name".into(), json!(name));
        saved.updated_at = Self::write(
            &mut tx,
            tenant,
            Collection::File,
            &saved,
            temporary,
            None,
        )
        .await?;
        if let Some(deadline) =
            saved.data.get("stagingExpiresAt").and_then(Value::as_i64)
        {
            sqlx::query("INSERT INTO pengolo_retention(tenant_id,kind,id,temporary,expires_at,staging_key) VALUES($1,'staging',$2,$3,$4,$5)").bind(tenant).bind(&saved.id).bind(temporary).bind(deadline).bind(saved.data.get("stagingKey").and_then(Value::as_str)).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(saved)
    }

    async fn save_project(
        &self,
        tenant: &str,
        record: &Record,
        expected: Option<i64>,
        access: &dyn AccessStore,
    ) -> Result<Record> {
        let lifecycle = self.database.tenant_lifecycle(tenant).await?;
        let previous = self
            .get(tenant, Collection::Project, &record.id, false)
            .await?;
        ensure!(
            previous.as_ref().map(|record| record.revision) == expected,
            "Project revision conflict"
        );
        ensure!(
            previous
                .as_ref()
                .is_none_or(|previous| previous.owner_id == record.owner_id),
            "Project ownership cannot change"
        );
        let roles = record
            .data
            .get("roles")
            .and_then(Value::as_array)
            .context("Invalid project roles")?;
        let mut tx = self.database.tenant(tenant).await?;
        for role in roles.iter().filter_map(Value::as_str) {
            let exists:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM iam_roles WHERE tenant_id=$1 AND role_name=$2 AND deleted_at IS NULL)").bind(tenant).bind(role).fetch_one(&mut *tx).await?;
            ensure!(exists, "Project role unavailable");
        }
        if let Some(previous) = &previous {
            let old_roles = previous
                .data
                .get("roles")
                .and_then(Value::as_array)
                .context("Invalid project roles")?
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>();
            let new_roles =
                roles.iter().filter_map(Value::as_str).collect::<Vec<_>>();
            // Expanding recipients upward also protects superiors who gained
            // access through a junior grant, including transitive inheritance.
            let revokes_superior: bool = sqlx::query_scalar(r#"
                WITH RECURSIVE superiors(role_name) AS (
                    SELECT edge.parent_role_name FROM iam_role_parents edge
                    JOIN iam_user_roles assignment ON assignment.tenant_id=edge.tenant_id AND assignment.role_name=edge.role_name
                    WHERE edge.tenant_id=$1 AND assignment.user_id=$2
                    UNION
                    SELECT edge.parent_role_name FROM iam_role_parents edge JOIN superiors ON edge.role_name=superiors.role_name WHERE edge.tenant_id=$1
                ), previous_recipients(role_name) AS (
                    SELECT unnest($3::text[])
                    UNION
                    SELECT edge.parent_role_name FROM iam_role_parents edge JOIN previous_recipients ON edge.role_name=previous_recipients.role_name WHERE edge.tenant_id=$1
                ), next_recipients(role_name) AS (
                    SELECT unnest($4::text[])
                    UNION
                    SELECT edge.parent_role_name FROM iam_role_parents edge JOIN next_recipients ON edge.role_name=next_recipients.role_name WHERE edge.tenant_id=$1
                ) SELECT EXISTS(SELECT FROM superiors JOIN previous_recipients USING(role_name) WHERE role_name NOT IN (SELECT role_name FROM next_recipients))
            "#).bind(tenant).bind(&previous.owner_id).bind(&old_roles).bind(&new_roles).fetch_one(&mut *tx).await?;
            ensure!(
                !revokes_superior,
                "Project permissions must preserve superior access"
            );
        }
        tx.commit().await?;
        let tuple = |relation: &str,
                     subject_type: &str,
                     subject_id: &str,
                     subject_relation: &str| AccessTuple {
            resource_type: "project".into(),
            resource_id: record.id.clone(),
            relation: relation.into(),
            subject_type: subject_type.into(),
            subject_id: subject_id.into(),
            subject_relation: subject_relation.into(),
        };
        let structural = [
            tuple("parent", "tenant", tenant, ""),
            tuple("owner", "user", &record.owner_id, ""),
        ];
        // Metadata and native projection share the IAM lifecycle lock. Every
        // read also checks current metadata, so a partial projection
        // fails closed.
        if previous.is_none() {
            access.set_many(tenant, &structural, true).await?;
        }
        let saved = self
            .save(tenant, Collection::Project, record, false, expected)
            .await?;
        if let Some(previous) = previous &&
            let Some(old) =
                previous.data.get("roles").and_then(Value::as_array)
        {
            for role in old.iter().filter_map(Value::as_str).filter(|role| {
                !roles.iter().any(|new| new.as_str() == Some(*role))
            }) {
                access
                    .set(
                        tenant,
                        &tuple("reader", "role", role, "member"),
                        false,
                    )
                    .await?;
            }
        }
        access.set_many(tenant, &structural, true).await?;
        for role in roles.iter().filter_map(Value::as_str) {
            access
                .set(tenant, &tuple("reader", "role", role, "member"), true)
                .await?;
        }
        lifecycle.release_now().await?;
        Ok(saved)
    }

    async fn expire_temporary(
        &self,
        tenant: &str,
        owner: &str,
        session: &str,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let ids:Vec<(String,String)>=sqlx::query_as("UPDATE pengolo_ephemeral_records SET expires_at=$1 WHERE tenant_id=$2 AND owner_id=$3 AND ((kind='chat' AND id=$4) OR (kind='generation' AND data->>'chatId'=$4) OR (kind='file' AND data->>'temporarySession'=$4)) RETURNING kind,id").bind(now()).bind(tenant).bind(owner).bind(session).fetch_all(&mut *tx).await?;
        for (kind, id) in ids {
            sqlx::query("UPDATE pengolo_retention SET expires_at=$1 WHERE tenant_id=$2 AND kind=$3 AND id=$4 AND temporary").bind(now()).bind(tenant).bind(&kind).bind(&id).execute(&mut *tx).await?;
            if kind == "generation" {
                sqlx::query("DELETE FROM pengolo_capabilities WHERE tenant_id=$1 AND generation_id=$2 AND temporary").bind(tenant).bind(id).execute(&mut *tx).await?;
            }
        }
        tx.commit().await?;
        Ok(())
    }

    async fn list_scoped(
        &self,
        query: RecordQuery<'_>,
    ) -> Result<Vec<Record>> {
        let RecordQuery {
            tenant,
            collection,
            project,
            owner,
            search,
            after,
            limit,
        } = query;
        let mut tx = self.database.tenant(tenant).await?;
        let rows:Vec<Value>=sqlx::query_scalar("SELECT jsonb_build_object('id',id,'ownerId',owner_id,'projectId',project_id,'expiresAt',expires_at,'revision',revision,'updatedAt',updated_at,'data',data) FROM pengolo_records WHERE tenant_id=$1 AND kind=$2 AND (expires_at IS NULL OR expires_at>$3) AND ($4::text IS NULL OR (updated_at,id)<(SELECT updated_at,id FROM pengolo_records WHERE tenant_id=$1 AND kind=$2 AND id=$4)) AND project_id IS NOT DISTINCT FROM $6 AND ($6::text IS NOT NULL OR owner_id=$7) AND ($8='' OR pengolo_filename_matches(data->>'name',$8)) ORDER BY updated_at DESC,id DESC LIMIT $5")
            .bind(tenant).bind(collection.as_str()).bind(now()).bind(after).bind(i64::try_from(limit.clamp(1,100))?).bind(project).bind(owner).bind(search).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        rows.into_iter()
            .map(|row| {
                serde_json::from_value(row).context("Invalid workspace record")
            })
            .collect()
    }

    async fn generation_capability(
        &self,
        tenant: &str,
        id: &str,
    ) -> Result<Option<String>> {
        let mut tx = self.database.tenant(tenant).await?;
        let token = sqlx::query_scalar("SELECT token FROM pengolo_capabilities WHERE tenant_id=$1 AND generation_id=$2").bind(tenant).bind(id).fetch_optional(&mut *tx).await?;
        tx.commit().await?;
        Ok(token)
    }

    async fn get(
        &self,
        tenant: &str,
        collection: Collection,
        id: &str,
        temporary: bool,
    ) -> Result<Option<Record>> {
        let mut tx = self.database.tenant(tenant).await?;
        let record =
            Self::read(&mut tx, collection, id, temporary, false).await?;
        tx.commit().await?;
        Ok(record)
    }

    async fn list(
        &self,
        tenant: &str,
        collection: Collection,
        after: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Record>> {
        let mut tx = self.database.tenant(tenant).await?;
        let rows: Vec<Value> = sqlx::query_scalar("SELECT jsonb_build_object('id',id,'ownerId',owner_id,'projectId',project_id,'expiresAt',expires_at,'revision',revision,'updatedAt',updated_at,'data',data) FROM pengolo_records WHERE tenant_id=$1 AND kind=$2 AND (expires_at IS NULL OR expires_at>$3) AND ($4::text IS NULL OR (updated_at,id) < (SELECT updated_at,id FROM pengolo_records WHERE tenant_id=$1 AND kind=$2 AND id=$4)) ORDER BY updated_at DESC,id DESC LIMIT $5")
            .bind(tenant).bind(collection.as_str()).bind(now()).bind(after).bind(i64::try_from(limit.clamp(1,100))?).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        rows.into_iter()
            .map(|row| {
                serde_json::from_value(row).context("Invalid workspace record")
            })
            .collect()
    }

    async fn save(
        &self,
        tenant: &str,
        collection: Collection,
        record: &Record,
        temporary: bool,
        expected: Option<i64>,
    ) -> Result<Record> {
        let mut tx = self.database.tenant(tenant).await?;
        let updated_at = Self::write(
            &mut tx, tenant, collection, record, temporary, expected,
        )
        .await?;
        if matches!(collection, Collection::Chat) {
            sqlx::query(AssertSqlSafe(format!("UPDATE {} SET project_id=$1 WHERE tenant_id=$2 AND kind='generation' AND data->>'chatId'=$3 AND project_id IS DISTINCT FROM $1",table(temporary)))).bind(&record.project_id).bind(tenant).bind(&record.id).execute(&mut *tx).await?;
        }
        let saved = Record {
            revision: expected.unwrap_or(0) + 1,
            updated_at,
            ..record.clone()
        };
        tx.commit().await?;
        Ok(saved)
    }

    async fn remove(
        &self,
        tenant: &str,
        collection: Collection,
        id: &str,
        temporary: bool,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        if matches!(collection, Collection::Chat) {
            let generations:Vec<String>=sqlx::query_scalar(AssertSqlSafe(format!("DELETE FROM {} WHERE tenant_id=$1 AND kind='generation' AND data->>'chatId'=$2 RETURNING id",table(temporary)))).bind(tenant).bind(id).fetch_all(&mut *tx).await?;
            for generation in generations {
                sqlx::query(AssertSqlSafe(format!(
                    "DELETE FROM {} WHERE tenant_id=$1 AND generation_id=$2",
                    output_table(temporary)
                )))
                .bind(tenant)
                .bind(&generation)
                .execute(&mut *tx)
                .await?;
                sqlx::query("DELETE FROM pengolo_capabilities WHERE tenant_id=$1 AND generation_id=$2").bind(tenant).bind(&generation).execute(&mut *tx).await?;
                sqlx::query("DELETE FROM pengolo_retention WHERE tenant_id=$1 AND id=$2 AND kind IN ('generation','lease') AND temporary=$3").bind(tenant).bind(&generation).bind(temporary).execute(&mut *tx).await?;
            }
        }
        sqlx::query(AssertSqlSafe(format!(
            "DELETE FROM {} WHERE tenant_id=$1 AND kind=$2 AND id=$3",
            table(temporary)
        )))
        .bind(tenant)
        .bind(collection.as_str())
        .bind(id)
        .execute(&mut *tx)
        .await?;
        if matches!(collection, Collection::Generation) {
            sqlx::query(AssertSqlSafe(format!(
                "DELETE FROM {} WHERE tenant_id=$1 AND generation_id=$2",
                output_table(temporary)
            )))
            .bind(tenant)
            .bind(id)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM pengolo_capabilities WHERE tenant_id=$1 AND generation_id=$2").bind(tenant).bind(id).execute(&mut *tx).await?;
            sqlx::query("DELETE FROM pengolo_retention WHERE tenant_id=$1 AND kind='lease' AND id=$2").bind(tenant).bind(id).execute(&mut *tx).await?;
        }
        sqlx::query("DELETE FROM pengolo_retention WHERE tenant_id=$1 AND kind=$2 AND id=$3 AND temporary=$4").bind(tenant).bind(collection.as_str()).bind(id).bind(temporary).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    async fn reserve(
        &self,
        tenant: &str,
        actor: &str,
        input: &SendInput,
        capability: &str,
    ) -> Result<Reservation> {
        let mut tx = self.database.tenant(tenant).await?;
        let id = uuid::Uuid::parse_str(&input.idempotency_key)?
            .simple()
            .to_string();
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 59031))",
        )
        .bind(format!("{tenant}/{id}"))
        .execute(&mut *tx)
        .await?;
        let serialized = serde_json::to_value(input)?;
        if let Some(generation) = Self::read(
            &mut tx,
            Collection::Generation,
            &id,
            input.temporary,
            false,
        )
        .await?
        {
            ensure!(
                generation.owner_id == actor &&
                    generation.data.get("input") == Some(&serialized),
                "Idempotency key conflicts with an earlier request"
            );
            let chat_id = generation
                .data
                .get("chatId")
                .and_then(Value::as_str)
                .context("Invalid generation chat")?;
            let chat = Self::read(
                &mut tx,
                Collection::Chat,
                chat_id,
                input.temporary,
                false,
            )
            .await?
            .context("Conversation unavailable")?;
            tx.commit().await?;
            return Ok(Reservation {
                chat,
                generation,
                created: false,
            });
        }
        let expires_at = input.temporary.then(|| now() + TEMPORARY_TTL_MS);
        let existing = match &input.chat_id {
            Some(id) => {
                Self::read(
                    &mut tx,
                    Collection::Chat,
                    id,
                    input.temporary,
                    true,
                )
                .await?
            },
            None => None,
        };
        ensure!(
            input.chat_id.is_none() || input.temporary || existing.is_some(),
            "Conversation unavailable"
        );
        let (mut chat, expected) = match existing {
            Some(chat) => {
                let revision = chat.revision;
                (chat, Some(revision))
            },
            None => (
                Record {
                    id: input
                        .chat_id
                        .clone()
                        .filter(|_| input.temporary)
                        .unwrap_or_else(|| {
                            uuid::Uuid::new_v4().simple().to_string()
                        }),
                    owner_id: actor.into(),
                    project_id: input.project_id.clone(),
                    expires_at,
                    revision: 1,
                    updated_at: now(),
                    data: json!({"title":input.prompt.chars().take(80).collect::<String>(),"pinned":false,"messages":[],"activeGeneration":null}),
                },
                None,
            ),
        };
        ensure!(
            chat.project_id == input.project_id,
            "Conversation project changed"
        );
        ensure!(
            chat.data.get("activeGeneration").is_none_or(Value::is_null),
            "A generation is already active"
        );
        let data =
            chat.data.as_object_mut().context("Invalid conversation")?;
        data.insert("activeGeneration".into(), json!(id));
        data.insert("generationContext".into(), json!({"graphContext":input.graph_context,"modelAlias":input.model_alias,"webSearch":input.web_search || input.web_query.is_some()}));
        let messages = data
            .get_mut("messages")
            .and_then(Value::as_array_mut)
            .context("Invalid conversation messages")?;
        ensure!(messages.len() < 1000, "Conversation message limit reached");
        messages.push(json!({"id":id,"role":"user","text":input.prompt,"fileIds":input.file_ids,"generationId":id,"status":"pending"}));
        Self::write(
            &mut tx,
            tenant,
            Collection::Chat,
            &chat,
            input.temporary,
            expected,
        )
        .await?;
        let generation = Record {
            id: id.clone(),
            owner_id: actor.into(),
            project_id: chat.project_id.clone(),
            expires_at: chat.expires_at,
            revision: 1,
            updated_at: now(),
            data: json!({"chatId":chat.id,"input":serialized,"status":"pending","sequence":0,"response":"","evidence":[],"leaseExpires":now()+45000}),
        };
        Self::write(
            &mut tx,
            tenant,
            Collection::Generation,
            &generation,
            input.temporary,
            None,
        )
        .await?;
        sqlx::query("INSERT INTO pengolo_capabilities(tenant_id,token,generation_id,temporary,expires_at) VALUES($1,$2,$3,$4,$5)").bind(tenant).bind(capability).bind(&id).bind(input.temporary).bind(now()+900000).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO pengolo_retention(tenant_id,kind,id,temporary,expires_at) VALUES($1,'lease',$2,$3,$4)").bind(tenant).bind(&id).bind(input.temporary).bind(now()+45000).execute(&mut *tx).await?;
        chat = Self::read(
            &mut tx,
            Collection::Chat,
            &chat.id,
            input.temporary,
            false,
        )
        .await?
        .context("Conversation unavailable")?;
        tx.commit().await?;
        Ok(Reservation {
            chat,
            generation,
            created: true,
        })
    }

    async fn append(
        &self,
        tenant: &str,
        id: &str,
        temporary: bool,
        event: &OutputEvent,
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let mut generation =
            Self::read(&mut tx, Collection::Generation, id, temporary, true)
                .await?
                .context("Generation unavailable")?;
        let sequence = generation
            .data
            .get("sequence")
            .and_then(Value::as_i64)
            .context("Invalid generation cursor")?;
        if event.sequence == -2 &&
            generation
                .data
                .get("leaseExpires")
                .and_then(Value::as_i64)
                .is_some_and(|deadline| deadline > now())
        {
            tx.commit().await?;
            return Ok(());
        }
        let allocated;
        let event = if event.sequence < 0 {
            ensure!(
                matches!(event.kind.as_str(), "failed" | "cancelled"),
                "Invalid terminal command"
            );
            allocated = OutputEvent {
                sequence: i32::try_from(sequence + 1)?,
                ..event.clone()
            };
            &allocated
        } else {
            event
        };
        if event.sequence > 0 && i64::from(event.sequence) <= sequence {
            let stored: Option<(String,String)> = sqlx::query_as(AssertSqlSafe(format!("SELECT kind,content FROM {} WHERE tenant_id=$1 AND generation_id=$2 AND sequence=$3", output_table(temporary)))).bind(tenant).bind(id).bind(event.sequence).fetch_optional(&mut *tx).await?;
            ensure!(
                stored
                    .as_ref()
                    .is_some_and(|(kind, content)| kind == &event.kind &&
                        content == &event.content),
                "Output replay conflict"
            );
            tx.commit().await?;
            return Ok(());
        }
        ensure!(
            matches!(
                generation.data.get("status").and_then(Value::as_str),
                Some("pending" | "running")
            ),
            "Generation is terminal"
        );
        ensure!(event.content.len() <= 65536, "Output batch exceeds limit");
        let revision = generation.revision;
        let data = generation
            .data
            .as_object_mut()
            .context("Invalid generation")?;
        data.insert("leaseExpires".into(), json!(now() + 45000));
        if event.kind != "heartbeat" {
            ensure!(
                i64::from(event.sequence) == sequence + 1,
                "Output cursor gap"
            );
            ensure!(
                matches!(
                    event.kind.as_str(),
                    "content" | "tool" | "completed" | "failed" | "cancelled"
                ),
                "Invalid output event"
            );
            data.insert("sequence".into(), json!(event.sequence));
            if event.kind == "content" {
                let response = data
                    .get_mut("response")
                    .context("Missing generation output")?;
                let mut text = response
                    .as_str()
                    .context("Invalid generation output")?
                    .to_owned();
                ensure!(
                    text.len() + event.content.len() <= 1048576,
                    "Output exceeds limit"
                );
                text.push_str(&event.content);
                *response = json!(text);
            }
            sqlx::query(AssertSqlSafe(format!("INSERT INTO {}(tenant_id,generation_id,sequence,kind,content) VALUES($1,$2,$3,$4,$5)", output_table(temporary)))).bind(tenant).bind(id).bind(event.sequence).bind(&event.kind).bind(&event.content).execute(&mut *tx).await?;
        }
        let terminal = matches!(
            event.kind.as_str(),
            "completed" | "failed" | "cancelled"
        );
        data.insert(
            "status".into(),
            json!(if terminal {
                event.kind.as_str()
            } else {
                "running"
            }),
        );
        if terminal {
            let chat_id = data
                .get("chatId")
                .and_then(Value::as_str)
                .context("Invalid generation chat")?;
            if let Some(mut chat) =
                Self::read(&mut tx, Collection::Chat, chat_id, temporary, true)
                    .await?
            {
                let chat_revision = chat.revision;
                let chat_data =
                    chat.data.as_object_mut().context("Invalid chat")?;
                chat_data.insert("activeGeneration".into(), Value::Null);
                let messages = chat_data
                    .get_mut("messages")
                    .and_then(Value::as_array_mut)
                    .context("Invalid messages")?;
                if let Some(message) = messages.iter_mut().find(|message| {
                    message.get("id").and_then(Value::as_str) == Some(id)
                }) && let Some(message) = message.as_object_mut()
                {
                    message.insert("status".into(), json!(event.kind));
                }
                messages.push(json!({"id":uuid::Uuid::new_v4().simple().to_string(),"role":"assistant","text":data.get("response"),"generationId":id,"status":event.kind,"evidence":data.get("evidence")}));
                Self::write(
                    &mut tx,
                    tenant,
                    Collection::Chat,
                    &chat,
                    temporary,
                    Some(chat_revision),
                )
                .await?;
            }
            sqlx::query("DELETE FROM pengolo_capabilities WHERE tenant_id=$1 AND generation_id=$2").bind(tenant).bind(id).execute(&mut *tx).await?;
            sqlx::query("DELETE FROM pengolo_retention WHERE tenant_id=$1 AND kind='lease' AND id=$2").bind(tenant).bind(id).execute(&mut *tx).await?;
        } else {
            sqlx::query("UPDATE pengolo_retention SET expires_at=$1 WHERE tenant_id=$2 AND kind='lease' AND id=$3 AND temporary=$4").bind(now()+45000).bind(tenant).bind(id).bind(temporary).execute(&mut *tx).await?;
        }
        Self::write(
            &mut tx,
            tenant,
            Collection::Generation,
            &generation,
            temporary,
            Some(revision),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn events(
        &self,
        tenant: &str,
        generation: &str,
        temporary: bool,
        after: i32,
    ) -> Result<Vec<OutputEvent>> {
        let mut tx = self.database.tenant(tenant).await?;
        let rows: Vec<(i32,String,String)> = sqlx::query_as(AssertSqlSafe(format!("SELECT sequence,kind,content FROM {} WHERE tenant_id=$1 AND generation_id=$2 AND sequence>$3 ORDER BY sequence LIMIT 64", output_table(temporary)))).bind(tenant).bind(generation).bind(after.max(0)).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        Ok(rows
            .into_iter()
            .map(|(sequence, kind, content)| OutputEvent {
                sequence,
                kind,
                content,
            })
            .collect())
    }

    async fn resolve_capability(
        &self,
        capability: &str,
    ) -> Result<(String, Record, bool)> {
        let (tenant, id, temporary) =
            self.database.pengolo_capability(capability).await?;
        let record = self
            .get(&tenant, Collection::Generation, &id, temporary)
            .await?
            .context("Generation unavailable")?;
        ensure!(
            matches!(
                record.data.get("status").and_then(Value::as_str),
                Some("pending" | "running")
            ),
            "Generation is terminal"
        );
        Ok((tenant, record, temporary))
    }

    async fn record_evidence(
        &self,
        tenant: &str,
        id: &str,
        temporary: bool,
        sources: &[EvidenceSource],
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let mut generation =
            Self::read(&mut tx, Collection::Generation, id, temporary, true)
                .await?
                .context("Generation unavailable")?;
        let revision = generation.revision;
        let evidence = generation
            .data
            .get_mut("evidence")
            .and_then(Value::as_array_mut)
            .context("Invalid evidence")?;
        for source in sources {
            let value = serde_json::to_value(source)?;
            if !evidence.contains(&value) {
                ensure!(evidence.len() < 512, "Evidence limit reached");
                evidence.push(value);
            }
        }
        Self::write(
            &mut tx,
            tenant,
            Collection::Generation,
            &generation,
            temporary,
            Some(revision),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }

    async fn expire_leases(&self, tenant: &str) -> Result<()> {
        for temporary in [false, true] {
            let mut tx = self.database.tenant(tenant).await?;
            let ids: Vec<String> = sqlx::query_scalar(AssertSqlSafe(format!("SELECT id FROM {} WHERE kind='generation' AND data->>'status' IN ('pending','running') AND (data->>'leaseExpires')::bigint<$1 LIMIT 100",table(temporary)))).bind(now()).fetch_all(&mut *tx).await?;
            tx.commit().await?;
            for id in ids {
                if self
                    .get(tenant, Collection::Generation, &id, temporary)
                    .await?
                    .is_some()
                {
                    let sequence = -2;
                    if let Err(_error) = self
                        .append(
                            tenant,
                            &id,
                            temporary,
                            &OutputEvent {
                                sequence,
                                kind: "failed".into(),
                                content: String::new(),
                            },
                        )
                        .await
                    {
                        tracing::warn!(
                            event.name = "pengolo.lease_expiry_race"
                        );
                    }
                }
            }
        }
        Ok(())
    }

    async fn expired_objects(
        &self,
        tenant: &str,
    ) -> Result<Vec<ObjectExpiry>> {
        let mut tx = self.database.tenant(tenant).await?;
        let rows:Vec<(String,String,bool)>=sqlx::query_as("SELECT kind,id,temporary FROM pengolo_retention WHERE tenant_id=$1 AND kind IN ('file','staging') AND expires_at<=$2 ORDER BY expires_at LIMIT 100").bind(tenant).bind(now()).fetch_all(&mut *tx).await?;
        tx.commit().await?;
        Ok(rows
            .into_iter()
            .map(|(kind, id, temporary)| ObjectExpiry {
                id,
                staging: kind == "staging",
                temporary,
            })
            .collect())
    }

    async fn cleanup_tenants(&self) -> Result<Vec<String>> {
        self.database.pengolo_cleanup_tenants().await
    }

    async fn expired(
        &self,
        tenant: &str,
    ) -> Result<Vec<(Collection, Record, bool)>> {
        let mut result = Vec::new();
        for temporary in [false, true] {
            let mut tx = self.database.tenant(tenant).await?;
            let rows: Vec<(String,Value)> = sqlx::query_as(AssertSqlSafe(format!("SELECT kind,jsonb_build_object('id',id,'ownerId',owner_id,'projectId',project_id,'expiresAt',expires_at,'revision',revision,'updatedAt',updated_at,'data',data) FROM {} WHERE expires_at<=$1 LIMIT 100",table(temporary)))).bind(now()).fetch_all(&mut *tx).await?;
            tx.commit().await?;
            for (kind, value) in rows {
                let collection = match kind.as_str() {
                    "file" => Collection::File,
                    "chat" => Collection::Chat,
                    "generation" => Collection::Generation,
                    _ => continue,
                };
                result.push((
                    collection,
                    serde_json::from_value(value)?,
                    temporary,
                ));
            }
        }
        Ok(result)
    }
}

#[cfg(test)]
struct EmptyAccess;
#[cfg(test)]
#[async_trait::async_trait]
impl crate::application::ports::access_store::AccessStore for EmptyAccess {
    async fn tuples(
        &self,
        _: &str,
        _: usize,
    ) -> Result<Vec<crate::application::ports::access_store::AccessTuple>>
    {
        Ok(Vec::new())
    }

    async fn set(
        &self,
        _: &str,
        _: &crate::application::ports::access_store::AccessTuple,
        _: bool,
    ) -> Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;

    use super::*;
    use crate::domain::pengolo::SendInput;

    #[tokio::test]
    async fn library_search_normalizes_accents_and_typos_before_scoped_pagination()
    -> Result<()> {
        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app; CREATE EXTENSION pg_trgm;".to_vec()).with_tag("18.6-alpine").start().await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        let store = PgPengoloStore::new(database);
        let file = Record {
            id: "a".repeat(32),
            owner_id: "alice".into(),
            project_id: None,
            expires_at: Some(now() + 60000),
            revision: 1,
            updated_at: 0,
            data: json!({"name":"Charte des Éxamens de l’Université.pdf", "status":"ready"}),
        };
        store
            .save("tenant_a", Collection::File, &file, false, None)
            .await?;
        for (tenant, id, owner, project, expiry) in [
            ("tenant_b", "b", "alice", None, now() + 60000),
            ("tenant_a", "c", "bob", None, now() + 60000),
            ("tenant_a", "d", "alice", Some("private"), now() + 60000),
            ("tenant_a", "e", "alice", None, now() - 1),
        ] {
            store
                .save(
                    tenant,
                    Collection::File,
                    &Record {
                        id: format!("{id:0>32}"),
                        owner_id: owner.into(),
                        project_id: project.map(str::to_owned),
                        expires_at: Some(expiry),
                        ..file.clone()
                    },
                    false,
                    None,
                )
                .await?;
        }
        for index in 0..12 {
            store
                .save(
                    "tenant_a",
                    Collection::File,
                    &Record {
                        id: format!("{index:032x}"),
                        data: json!({"name":"Unrelated archive.pdf"}),
                        ..file.clone()
                    },
                    false,
                    None,
                )
                .await?;
        }
        for query in [
            "EXAMENS",
            "éxamens",
            "e\u{301}xamens",
            "universite",
            "charte exmaens",
            "charte examens",
        ] {
            let page = store
                .list_scoped(RecordQuery {
                    tenant: "tenant_a",
                    collection: Collection::File,
                    project: None,
                    owner: "alice",
                    search: query,
                    after: None,
                    limit: 1,
                })
                .await?;
            assert_eq!(
                page.first().map(|record| record.id.as_str()),
                Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                "query: {query}"
            );
            assert_eq!(page.len(), 1);
        }
        let page = store
            .list_scoped(RecordQuery {
                tenant: "tenant_a",
                collection: Collection::File,
                project: None,
                owner: "alice",
                search: "exmaens",
                after: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                limit: 1,
            })
            .await?;
        assert!(page.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn project_edits_preserve_superior_access_under_the_lifecycle_lock()
    -> Result<()> {
        use crate::adapters::outbound::database::iam::PgIamStore;
        use crate::application::ports::iam_store::IamStore;

        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;".to_vec()).with_tag("18.6-alpine").start().await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        let iam = PgIamStore::new(database.clone());
        for tenant in ["tenant_a", "tenant_b"] {
            iam.create_user(tenant, "owner", true).await?;
            iam.create_roles(tenant, &["staff", "lead", "director", "other"])
                .await?;
            iam.assign_role_to_user(tenant, "owner", "staff").await?;
        }
        iam.set_role_parent("tenant_a", "staff", "lead", true)
            .await?;
        iam.set_role_parent("tenant_a", "lead", "director", true)
            .await?;
        let store = PgPengoloStore::new(database);
        let mut project = Record {
            id: "a".repeat(32),
            owner_id: "owner".into(),
            project_id: None,
            expires_at: None,
            revision: 1,
            updated_at: 0,
            data: json!({"name":"Investigation","shared":true,"roles":["staff","other"]}),
        };
        store
            .save_project("tenant_a", &project, None, &EmptyAccess)
            .await?;
        project.data =
            json!({"name":"Renamed","shared":true,"roles":["other"]});
        assert!(
            store
                .save_project("tenant_a", &project, Some(1), &EmptyAccess)
                .await
                .is_err(),
            "Removing a junior grant must not revoke its direct or transitive superiors"
        );
        let unchanged = store
            .get("tenant_a", Collection::Project, &project.id, false)
            .await?
            .context("Project missing")?;
        assert_eq!(unchanged.revision, 1);
        assert_eq!(unchanged.data.get("name"), Some(&json!("Investigation")));
        project.data =
            json!({"name":"Renamed","shared":true,"roles":["lead","other"]});
        let saved = store
            .save_project("tenant_a", &project, Some(1), &EmptyAccess)
            .await?;
        assert_eq!(saved.revision, 2);
        project.data = json!({"name":"Renamed","shared":true,"roles":["director","other"]});
        assert!(
            store
                .save_project("tenant_a", &project, Some(2), &EmptyAccess)
                .await
                .is_err(),
            "Preserving only the highest role must not revoke an intermediate superior"
        );
        store
            .save_project("tenant_b", &project, None, &EmptyAccess)
            .await?;
        project.data =
            json!({"name":"Tenant isolated","shared":true,"roles":["other"]});
        assert!(
            store
                .save_project("tenant_b", &project, Some(1), &EmptyAccess)
                .await
                .is_ok()
        );
        Ok(())
    }

    #[tokio::test]
    async fn first_turn_is_atomic_idempotent_and_temporary_state_is_separate()
    -> anyhow::Result<()> {
        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;".to_vec()).with_tag("18.6-alpine").start().await?;
        let host = container.get_host().await?;
        let port = container.get_host_port_ipv4(5432).await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{host}:{port}/postgres"
        ))
        .await?;
        database.verify_security().await?;
        let store = PgPengoloStore::new(database);
        let input = SendInput::parse(
            serde_json::json!({"idempotencyKey":"12345678-1234-1234-1234-123456789abc","prompt":"hello","modelAlias":"local"}),
        )?;
        let first = store
            .reserve("tenant_a", "actor", &input, &"a".repeat(64))
            .await?;
        assert!(first.created);
        let retry = store
            .reserve("tenant_a", "actor", &input, &"b".repeat(64))
            .await?;
        assert!(!retry.created);
        assert_eq!(first.chat.id, retry.chat.id);
        assert_eq!(
            store
                .list("tenant_a", Collection::Chat, None, 50)
                .await?
                .len(),
            1
        );
        assert!(
            store
                .get("tenant_b", Collection::Chat, &first.chat.id, false)
                .await?
                .is_none()
        );
        let mut changed = input.clone();
        changed.prompt = "different".into();
        assert!(
            store
                .reserve("tenant_a", "actor", &changed, &"a".repeat(64))
                .await
                .is_err()
        );
        changed.idempotency_key = uuid::Uuid::new_v4().to_string();
        changed.temporary = true;
        let transient = store
            .reserve("tenant_a", "actor", &changed, &"c".repeat(64))
            .await?;
        assert!(
            store
                .get("tenant_a", Collection::Chat, &transient.chat.id, false)
                .await?
                .is_none()
        );
        assert_eq!(
            store
                .list("tenant_a", Collection::Chat, None, 50)
                .await?
                .len(),
            1
        );
        let event = OutputEvent {
            sequence: 1,
            kind: "content".into(),
            content: "answer".into(),
        };
        store
            .append("tenant_a", &first.generation.id, false, &event)
            .await?;
        store
            .append("tenant_a", &first.generation.id, false, &event)
            .await?;
        assert_eq!(
            store
                .events("tenant_a", &first.generation.id, false, 0)
                .await?
                .len(),
            1
        );
        assert!(
            store
                .append(
                    "tenant_a",
                    &first.generation.id,
                    false,
                    &OutputEvent {
                        sequence: 3,
                        ..event
                    }
                )
                .await
                .is_err()
        );
        store
            .append(
                "tenant_a",
                &first.generation.id,
                false,
                &OutputEvent {
                    sequence: 2,
                    kind: "completed".into(),
                    content: String::new(),
                },
            )
            .await?;
        let mut moved = store
            .get("tenant_a", Collection::Chat, &first.chat.id, false)
            .await?
            .context("Missing chat")?;
        moved.project_id = Some("project".into());
        store
            .save(
                "tenant_a",
                Collection::Chat,
                &moved,
                false,
                Some(moved.revision),
            )
            .await?;
        assert_eq!(
            store
                .get(
                    "tenant_a",
                    Collection::Generation,
                    &first.generation.id,
                    false
                )
                .await?
                .context("Missing generation")?
                .project_id
                .as_deref(),
            Some("project")
        );
        store
            .remove("tenant_a", Collection::Chat, &first.chat.id, false)
            .await?;
        assert!(
            store
                .get(
                    "tenant_a",
                    Collection::Generation,
                    &first.generation.id,
                    false
                )
                .await?
                .is_none()
        );
        assert!(
            store
                .events("tenant_a", &first.generation.id, false, 0)
                .await?
                .is_empty()
        );
        store
            .expire_temporary("tenant_a", "actor", &transient.chat.id)
            .await?;
        assert!(
            store
                .get("tenant_a", Collection::Chat, &transient.chat.id, true)
                .await?
                .is_none()
        );
        assert!(store.resolve_capability(&"c".repeat(64)).await.is_err());
        let project = Record {
            id: "a".repeat(32),
            owner_id: "actor".into(),
            project_id: None,
            expires_at: None,
            revision: 1,
            updated_at: 0,
            data: json!({"roles":[],"shared":false}),
        };
        let file = Record {
            id: "d".repeat(32),
            owner_id: "actor".into(),
            project_id: None,
            expires_at: Some(now() + 10000),
            revision: 1,
            updated_at: 0,
            data: json!({"name":"report.pdf","originalName":"report.pdf"}),
        };
        let one = store.reserve_file("tenant_a", &file, false).await?;
        let two = store
            .reserve_file(
                "tenant_a",
                &Record {
                    id: "e".repeat(32),
                    ..file.clone()
                },
                false,
            )
            .await?;
        assert_eq!(
            one.data.get("name").and_then(Value::as_str),
            Some("report.pdf")
        );
        assert_eq!(
            two.data.get("name").and_then(Value::as_str),
            Some("report (1).pdf")
        );
        let saved = store
            .save_project("tenant_a", &project, None, &EmptyAccess)
            .await?;
        assert_eq!(saved.revision, 1);
        assert!(
            store
                .save_project("tenant_a", &project, Some(2), &EmptyAccess)
                .await
                .is_err()
        );
        Ok(())
    }
    #[derive(Default)]
    struct DelayedObjects {
        started: tokio::sync::Notify,
        released: tokio::sync::Notify,
        deletions: std::sync::atomic::AtomicUsize,
    }

    #[async_trait::async_trait]
    impl crate::application::ports::pengolo_store::PengoloObjects
        for DelayedObjects
    {
        async fn upload_url(
            &self,
            _: &crate::application::ports::pengolo_store::StagedFile<'_>,
        ) -> Result<(String, std::collections::BTreeMap<String, String>)>
        {
            anyhow::bail!("Unexpected upload signing")
        }

        async fn finalize(
            &self,
            _: &crate::application::ports::pengolo_store::StagedFile<'_>,
        ) -> Result<i64> {
            self.started.notify_one();
            self.released.notified().await;
            Ok(now())
        }

        async fn delete_staging(&self, _: &str) -> Result<()> {
            Ok(())
        }

        async fn read(
            &self,
            _: &str,
        ) -> Result<crate::application::ports::pengolo_store::FileChunks>
        {
            anyhow::bail!("Unexpected file read")
        }

        async fn download_url(&self, _: &str, _: u64) -> Result<String> {
            anyhow::bail!("Unexpected download signing")
        }

        async fn delete(&self, _: &str) -> Result<()> {
            self.deletions
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(())
        }
    }

    #[tokio::test]
    async fn file_promotion_returns_persisted_metadata_for_idempotent_retries()
    -> Result<()> {
        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;".to_vec()).with_tag("18.6-alpine").start().await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        database.verify_security().await?;
        let mut tx = database.tenant("tenant_a").await?;
        sqlx::query("CREATE FUNCTION delay_pengolo_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.02); RETURN NEW; END $$")
            .execute(&mut *tx).await?;
        for table in ["pengolo_records", "pengolo_ephemeral_records"] {
            sqlx::query(AssertSqlSafe(format!("CREATE TRIGGER delayed_write AFTER INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION delay_pengolo_write()")))
                .execute(&mut *tx).await?;
        }
        tx.commit().await?;
        let store = PgPengoloStore::new(database);
        let objects = DelayedObjects::default();
        for temporary in [false, true] {
            let record = Record {
                id: "a".repeat(32),
                owner_id: "actor".into(),
                project_id: None,
                expires_at: Some(now() + 60000),
                revision: 1,
                updated_at: 0,
                data: json!({"name":"proof.txt","mime":"text/plain","size":5,"key":"tenant_a/actor/pengolo/proof","stagingKey":"tenant_a/actor/pengolo/staged","stagingExpiresAt":now()+60000,"checksum":"checksum","status":"uploading"}),
            };
            let record =
                store.reserve_file("tenant_a", &record, temporary).await?;
            let reserved = store
                .get("tenant_a", Collection::File, &record.id, temporary)
                .await?
                .context("Missing reserved file")?;
            assert_eq!(
                serde_json::to_value(&record)?,
                serde_json::to_value(&reserved)?
            );
            let file = StagedFile {
                tenant: "tenant_a",
                owner: "actor",
                source: "tenant_a/actor/pengolo/staged",
                destination: "tenant_a/actor/pengolo/proof",
                mime: "text/plain",
                size: 5,
                name: "proof.txt",
                checksum: "checksum",
            };
            objects.released.notify_one();
            let promoted = store
                .promote_file("tenant_a", &record, temporary, &file, &objects)
                .await?;
            let persisted = store
                .get("tenant_a", Collection::File, &record.id, temporary)
                .await?
                .context("Missing promoted file")?;
            assert_eq!(
                serde_json::to_value(&promoted)?,
                serde_json::to_value(&persisted)?
            );
            let retried = store
                .promote_file("tenant_a", &record, temporary, &file, &objects)
                .await?;
            assert_eq!(
                serde_json::to_value(&promoted)?,
                serde_json::to_value(&retried)?
            );
            let saved = store
                .save(
                    "tenant_a",
                    Collection::File,
                    &promoted,
                    temporary,
                    Some(promoted.revision),
                )
                .await?;
            let persisted = store
                .get("tenant_a", Collection::File, &record.id, temporary)
                .await?
                .context("Missing saved file")?;
            assert_eq!(
                serde_json::to_value(&saved)?,
                serde_json::to_value(&persisted)?
            );
        }
        Ok(())
    }

    #[tokio::test]
    async fn file_expiration_cannot_race_promotion_or_leave_an_orphan()
    -> Result<()> {
        use std::sync::Arc;
        use std::sync::atomic::Ordering;
        use std::time::Duration;

        use crate::application::ports::pengolo_store::StagedFile;
        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;".to_vec()).with_tag("18.6-alpine").start().await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        database.verify_security().await?;
        let store = Arc::new(PgPengoloStore::new(database));
        let objects = Arc::new(DelayedObjects::default());
        let id = "f".repeat(32);
        let record = Record {
            id: id.clone(),
            owner_id: "actor".into(),
            project_id: None,
            expires_at: Some(now() + 1000),
            revision: 1,
            updated_at: now(),
            data: json!({"name":"proof.txt","mime":"text/plain","size":5,"key":"tenant_a/actor/pengolo/proof","stagingKey":"tenant_a/actor/pengolo/staged","stagingExpiresAt":now()+1000,"checksum":"checksum","status":"uploading"}),
        };
        let record = store.reserve_file("tenant_a", &record, false).await?;
        let worker_store = store.clone();
        let worker_objects = objects.clone();
        let promotion = tokio::spawn(async move {
            worker_store
                .promote_file(
                    "tenant_a",
                    &record,
                    false,
                    &StagedFile {
                        tenant: "tenant_a",
                        owner: "actor",
                        source: "tenant_a/actor/pengolo/staged",
                        destination: "tenant_a/actor/pengolo/proof",
                        mime: "text/plain",
                        size: 5,
                        name: "proof.txt",
                        checksum: "checksum",
                    },
                    worker_objects.as_ref(),
                )
                .await
        });
        objects.started.notified().await;
        tokio::time::sleep(Duration::from_millis(1100)).await;
        let cleaner_store = store.clone();
        let cleaner_objects = objects.clone();
        let cleaner_id = id.clone();
        let mut cleanup = tokio::spawn(async move {
            cleaner_store
                .purge_file(
                    "tenant_a",
                    &cleaner_id,
                    false,
                    false,
                    cleaner_objects.as_ref(),
                )
                .await
        });
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut cleanup)
                .await
                .is_err()
        );
        objects.released.notify_one();
        assert!(
            promotion.await?.is_err(),
            "Expired reservations cannot become ready"
        );
        assert!(cleanup.await??);
        assert_eq!(objects.deletions.load(Ordering::SeqCst), 1);
        assert!(
            store
                .get("tenant_a", Collection::File, &id, false)
                .await?
                .is_none()
        );
        Ok(())
    }
}
