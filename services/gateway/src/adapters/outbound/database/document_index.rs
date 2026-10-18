//! Document projections publish atomically and join live source state before
//! retrieval.

use anyhow::{Result, ensure};
use serde_json::{Value, json};
use sqlx::postgres::PgArguments;
use sqlx::query::QueryScalar;
use sqlx::{AssertSqlSafe, Postgres};

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::document_index::{
    Candidates, DocumentIndex, FactHit, IndexJob, IndexScope,
};
use crate::domain::document_retrieval::{
    Feature, SearchHit, SearchMode, Unit, validate_index,
};

pub struct PgDocumentIndex {
    database: Database,
}

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

impl PgDocumentIndex {
    pub fn new(database: Database) -> Self {
        Self { database }
    }
}

const SCOPE: &str = "FROM retrieval_units u JOIN pengolo_records r ON r.tenant_id=u.tenant_id AND r.kind='file' AND r.id=u.source_id WHERE u.tenant_id=$1 AND $9::text IS NOT NULL AND r.data->>'status'='ready' AND r.expires_at>$8 AND u.source_checksum=r.data->>'checksum' AND u.revision=r.data#>>'{retrieval,revision}' AND (($3::text IS NULL AND r.project_id IS NULL AND r.owner_id=$2) OR ($3::text IS NOT NULL AND r.project_id=$3)) AND (cardinality($4::text[])=0 OR r.id=ANY($4)) AND (cardinality($5::text[])=0 OR r.data->>'mime'=ANY($5)) AND ($6::bigint IS NULL OR r.updated_at >= $6) AND ($7::bigint IS NULL OR r.updated_at <= $7)";
const HIT: &str = "'file',jsonb_build_object('id',r.id,'ownerId',r.owner_id,'projectId',r.project_id,'expiresAt',r.expires_at,'revision',r.revision,'updatedAt',r.updated_at,'data',r.data-'key'-'stagingKey'),'sourceId',u.source_id,'sourceChecksum',u.source_checksum,'unitId',u.id,'filename',r.data->>'name','mime',r.data->>'mime','text',u.text,'page',(u.location->>'page')::integer,'start',(u.location->>'start')::bigint,'end',(u.location->>'end')::bigint,'relevance',NULL,'graphScore',NULL,'facts',u.facts";

fn scoped<'a>(
    sql: &'a str,
    scope: IndexScope<'a>,
) -> QueryScalar<'a, Postgres, Value, PgArguments> {
    sqlx::query_scalar(AssertSqlSafe(sql))
        .bind(scope.tenant)
        .bind(scope.owner)
        .bind(scope.input.project_id.as_deref())
        .bind(&scope.input.file_ids)
        .bind(&scope.input.mime_types)
        .bind(scope.input.updated_after)
        .bind(scope.input.updated_before)
        .bind(now())
        .bind(&scope.input.query)
}

#[async_trait::async_trait]
impl DocumentIndex for PgDocumentIndex {
    #[tracing::instrument(skip_all)]
    async fn enqueue(&self, tenant: &str, source: &str) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let result = sqlx::query("INSERT INTO retrieval_jobs(tenant_id,source_id,source_checksum) SELECT tenant_id,id,data->>'checksum' FROM pengolo_records WHERE tenant_id=$1 AND kind='file' AND id=$2 AND data->>'status'='ready' AND expires_at>$3 ON CONFLICT(tenant_id,source_id) DO UPDATE SET source_checksum=EXCLUDED.source_checksum,attempts=CASE WHEN retrieval_jobs.source_checksum=EXCLUDED.source_checksum THEN retrieval_jobs.attempts ELSE 0 END,next_attempt=CASE WHEN retrieval_jobs.source_checksum=EXCLUDED.source_checksum THEN retrieval_jobs.next_attempt ELSE 0 END,lease=CASE WHEN retrieval_jobs.source_checksum=EXCLUDED.source_checksum THEN retrieval_jobs.lease ELSE NULL END,lease_expires=CASE WHEN retrieval_jobs.source_checksum=EXCLUDED.source_checksum THEN retrieval_jobs.lease_expires ELSE 0 END")
            .bind(tenant).bind(source).bind(now()).execute(&mut *tx).await?;
        ensure!(
            result.rows_affected() == 1,
            "Document unavailable for indexing"
        );
        tx.commit().await?;
        Ok(())
    }

    async fn tenants_with_work(&self) -> Result<Vec<String>> {
        self.database.document_index_tenants().await
    }

    async fn renew(&self, tenant: &str, job: &IndexJob) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let renewed=sqlx::query("UPDATE retrieval_jobs j SET lease_expires=$4+300000 FROM pengolo_records r WHERE j.tenant_id=$1 AND j.source_id=$2 AND j.lease=$3 AND j.lease_expires>$4 AND r.tenant_id=j.tenant_id AND r.kind='file' AND r.id=j.source_id AND r.data->>'checksum'=j.source_checksum AND r.expires_at>$4 AND r.data->>'status'='ready'").bind(tenant).bind(&job.source_id).bind(&job.lease).bind(now()).execute(&mut *tx).await?;
        ensure!(renewed.rows_affected() == 1, "Document indexing lease lost");
        tx.commit().await?;
        Ok(())
    }

    #[tracing::instrument(skip_all)]
    async fn claim(&self, tenant: &str) -> Result<Option<IndexJob>> {
        let mut tx = self.database.tenant(tenant).await?;
        let value: Option<(String,String,Value)> = sqlx::query_as("SELECT j.source_id,j.source_checksum,jsonb_build_object('id',r.id,'ownerId',r.owner_id,'projectId',r.project_id,'expiresAt',r.expires_at,'revision',r.revision,'updatedAt',r.updated_at,'data',r.data) FROM retrieval_jobs j JOIN pengolo_records r ON r.tenant_id=j.tenant_id AND r.kind='file' AND r.id=j.source_id WHERE j.tenant_id=$1 AND j.attempts<8 AND j.next_attempt<=$2 AND j.lease_expires<=$2 AND r.expires_at>$2 AND r.data->>'status'='ready' AND r.data->>'checksum'=j.source_checksum ORDER BY j.next_attempt,j.source_id FOR UPDATE OF j SKIP LOCKED LIMIT 1").bind(tenant).bind(now()).fetch_optional(&mut *tx).await?;
        let Some((source_id, checksum, file)) = value else {
            tx.commit().await?;
            return Ok(None);
        };
        let lease = uuid::Uuid::new_v4().simple().to_string();
        sqlx::query("UPDATE retrieval_jobs SET lease=$3,lease_expires=$4,attempts=attempts+1 WHERE tenant_id=$1 AND source_id=$2").bind(tenant).bind(&source_id).bind(&lease).bind(now()+300000).execute(&mut *tx).await?;
        sqlx::query("DELETE FROM retrieval_units u USING pengolo_records r WHERE u.tenant_id=$1 AND u.source_id=$2 AND r.tenant_id=u.tenant_id AND r.kind='file' AND r.id=u.source_id AND u.revision IS DISTINCT FROM r.data#>>'{retrieval,revision}'").bind(tenant).bind(&source_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(Some(IndexJob {
            source_id,
            checksum,
            lease,
            file: serde_json::from_value(file)?,
        }))
    }

    #[tracing::instrument(skip_all)]
    async fn stage(
        &self,
        tenant: &str,
        job: &IndexJob,
        units: &[Unit],
        features: &[Feature],
        profile: Option<&str>,
    ) -> Result<()> {
        validate_index(&job.source_id, units, features)?;
        ensure!(
            features.is_empty() ||
                profile.is_some_and(|profile| profile.len() == 64 &&
                    profile
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit())),
            "Invalid indexing profile"
        );
        let mut tx = self.database.tenant(tenant).await?;
        let active: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM retrieval_jobs j JOIN pengolo_records r ON r.tenant_id=j.tenant_id AND r.kind='file' AND r.id=j.source_id WHERE j.tenant_id=$1 AND j.source_id=$2 AND j.lease=$3 AND j.lease_expires>$4 AND r.expires_at>$4 AND r.data->>'checksum'=$5 AND r.data->>'status'='ready')").bind(tenant).bind(&job.source_id).bind(&job.lease).bind(now()).bind(&job.checksum).fetch_one(&mut *tx).await?;
        ensure!(active, "Indexing lease or source expired");
        sqlx::query("INSERT INTO retrieval_units(tenant_id,source_id,source_checksum,revision,id,ordinal,text,location,facts) SELECT $1,$2,$3,$4,x.id,x.ordinal,x.text,jsonb_build_object('start',x.start,'end',x.\"end\",'page',x.page),x.facts FROM jsonb_to_recordset($5) AS x(id text,ordinal integer,text text,start bigint,\"end\" bigint,page integer,facts jsonb) ON CONFLICT(tenant_id,id,revision) DO UPDATE SET facts=EXCLUDED.facts")
            .bind(tenant).bind(&job.source_id).bind(&job.checksum).bind(&job.lease).bind(serde_json::to_value(units)?).execute(&mut *tx).await?;
        sqlx::query("UPDATE retrieval_units SET phrases=ARRAY(SELECT DISTINCT lower(phrase.value) FROM jsonb_array_elements(facts) fact CROSS JOIN LATERAL (VALUES(fact->>'subject'),(fact->>'object')) phrase(value)) WHERE tenant_id=$1 AND source_id=$2 AND revision=$3 AND id=ANY($4::text[])").bind(tenant).bind(&job.source_id).bind(&job.lease).bind(units.iter().map(|unit|unit.id.as_str()).collect::<Vec<_>>()).execute(&mut *tx).await?;
        if !features.is_empty() {
            sqlx::query("INSERT INTO retrieval_features(tenant_id,unit_id,revision,ordinal,profile_id,embedding) SELECT $1,x.unit_id,$2,x.ordinal,$3,x.embedding FROM jsonb_to_recordset($4) AS x(unit_id text,ordinal integer,embedding real[]) ON CONFLICT(tenant_id,unit_id,revision,ordinal) DO UPDATE SET embedding=EXCLUDED.embedding,profile_id=EXCLUDED.profile_id")
                .bind(tenant).bind(&job.lease).bind(profile).bind(serde_json::to_value(features)?).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(())
    }

    #[tracing::instrument(skip_all)]
    async fn publish(
        &self,
        tenant: &str,
        job: &IndexJob,
        profile: Option<&str>,
        warnings: &[String],
    ) -> Result<()> {
        let mut tx = self.database.tenant(tenant).await?;
        let live: Option<String> = sqlx::query_scalar("SELECT r.id FROM pengolo_records r JOIN retrieval_jobs j ON j.tenant_id=r.tenant_id AND j.source_id=r.id WHERE r.tenant_id=$1 AND r.kind='file' AND r.id=$2 AND r.data->>'checksum'=$3 AND r.data->>'status'='ready' AND r.expires_at>$4 AND j.lease=$5 AND j.lease_expires>$4 FOR UPDATE OF r,j")
            .bind(tenant).bind(&job.source_id).bind(&job.checksum).bind(now()).bind(&job.lease).fetch_optional(&mut *tx).await?;
        ensure!(live.is_some(), "Stale document indexing result");
        sqlx::query("UPDATE pengolo_records SET data=jsonb_set(data,'{retrieval}',$3),revision=revision+1 WHERE tenant_id=$1 AND kind='file' AND id=$2")
            .bind(tenant).bind(&job.source_id).bind(json!({"revision":job.lease,"profileId":profile,"status":"ready","warnings":warnings})).execute(&mut *tx).await?;
        sqlx::query("DELETE FROM retrieval_units WHERE tenant_id=$1 AND source_id=$2 AND revision<>$3").bind(tenant).bind(&job.source_id).bind(&job.lease).execute(&mut *tx).await?;
        let transient = warnings.iter().any(|warning| {
            matches!(
                warning.as_str(),
                "embeddings_unavailable" |
                    "extraction_unavailable" |
                    "graph_extraction_unavailable"
            )
        });
        if transient {
            sqlx::query("UPDATE retrieval_jobs SET lease=NULL,lease_expires=0,next_attempt=$4+least(3600000,(1000*power(2,attempts))::bigint) WHERE tenant_id=$1 AND source_id=$2 AND lease=$3").bind(tenant).bind(&job.source_id).bind(&job.lease).bind(now()).execute(&mut *tx).await?;
        } else {
            sqlx::query("DELETE FROM retrieval_jobs WHERE tenant_id=$1 AND source_id=$2 AND lease=$3").bind(tenant).bind(&job.source_id).bind(&job.lease).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        tracing::info!(event.name = "document.index.published");
        Ok(())
    }

    async fn fail(
        &self,
        tenant: &str,
        job: &IndexJob,
        code: &str,
    ) -> Result<()> {
        ensure!(
            matches!(code, "unsupported" | "unavailable" | "invalid"),
            "Invalid indexing failure"
        );
        let mut tx = self.database.tenant(tenant).await?;
        sqlx::query("UPDATE retrieval_jobs SET lease=NULL,lease_expires=0,next_attempt=$4+least(3600000,(1000*power(2,attempts))::bigint) WHERE tenant_id=$1 AND source_id=$2 AND lease=$3").bind(tenant).bind(&job.source_id).bind(&job.lease).bind(now()).execute(&mut *tx).await?;
        sqlx::query("UPDATE pengolo_records r SET data=jsonb_set(r.data,'{retrieval}',jsonb_build_object('status','failed','warnings',jsonb_build_array($3::text))),revision=revision+1 FROM retrieval_jobs j WHERE r.tenant_id=$1 AND r.kind='file' AND r.id=$2 AND j.tenant_id=r.tenant_id AND j.source_id=r.id AND j.attempts>=8 AND r.data#>>'{retrieval,status}' IS DISTINCT FROM 'ready'").bind(tenant).bind(&job.source_id).bind(code).execute(&mut *tx).await?;
        tx.commit().await?;
        tracing::warn!(event.name = "document.index.deferred", failure = code);
        Ok(())
    }

    #[tracing::instrument(skip_all)]
    async fn expand(
        &self,
        scope: IndexScope<'_>,
        seeds: &[FactHit],
    ) -> Result<Vec<SearchHit>> {
        ensure!(seeds.len() <= 5, "Graph seed budget exceeded");
        let mut weights = std::collections::HashMap::<String, f64>::new();
        for seed in seeds {
            ensure!(
                seed.similarity.is_finite() &&
                    (0.0..=1.0).contains(&seed.similarity),
                "Invalid graph seed"
            );
            for phrase in [&seed.fact.subject, &seed.fact.object] {
                weights
                    .entry(phrase.to_lowercase())
                    .and_modify(|weight| *weight = weight.max(seed.similarity))
                    .or_insert(seed.similarity);
            }
        }
        let mut tx = self.database.tenant(scope.tenant).await?;
        let mut visited = Vec::<String>::new();
        let mut result = Vec::new();
        let seed_ids: Vec<&str> =
            seeds.iter().map(|seed| seed.passage_id.as_str()).collect();
        for _depth in 0..4 {
            if weights.is_empty() || result.len() >= 128 {
                break;
            }
            let keys: Vec<&str> = weights.keys().map(String::as_str).collect();
            let score = "(SELECT sum(weight.value::float8)/sqrt(greatest(cardinality(u.phrases),1)) FROM jsonb_each_text($11::jsonb) weight WHERE weight.key=ANY(u.phrases))";
            let sql = format!(
                "SELECT jsonb_build_object({HIT},'similarity',NULL,'rankScore',COALESCE({score},0.)) {SCOPE} AND (u.phrases && $10::text[] OR u.id=ANY($12::text[])) AND NOT (u.id=ANY($13::text[])) ORDER BY (u.id=ANY($12::text[])) DESC,COALESCE({score},0.) DESC,u.id LIMIT $14"
            );
            let rows = scoped(&sql, scope)
                .bind(&keys)
                .bind(serde_json::to_value(&weights)?)
                .bind(&seed_ids)
                .bind(&visited)
                .bind(i64::try_from((128 - result.len()).min(32))?)
                .fetch_all(&mut *tx)
                .await?;
            let mut next = std::collections::HashMap::<String, f64>::new();
            for value in rows {
                let hit: SearchHit = serde_json::from_value(value)?;
                for fact in &hit.facts {
                    for phrase in [&fact.subject, &fact.object] {
                        next.entry(phrase.to_lowercase())
                            .and_modify(|weight| {
                                *weight = weight.max(hit.rank_score * 0.5)
                            })
                            .or_insert(hit.rank_score * 0.5);
                    }
                }
                visited.push(hit.unit_id.clone());
                result.push(hit);
            }
            weights = next;
        }
        tx.commit().await?;
        Ok(result)
    }

    #[tracing::instrument(skip_all)]
    async fn candidates(
        &self,
        scope: IndexScope<'_>,
        vector: Option<&[f32]>,
        profile: Option<&str>,
    ) -> Result<Candidates> {
        let mut tx = self.database.tenant(scope.tenant).await?;
        let (bm25,diskann): (bool,bool) = sqlx::query_as("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_textsearch'), EXISTS(SELECT 1 FROM pg_extension WHERE extname='vectorscale')").fetch_one(&mut *tx).await?;
        let lexical_score = if bm25 {
            "u.text <@> to_bm25query($9,'retrieval_units_bm25')"
        } else {
            "ts_rank_cd(to_tsvector('french',u.text),websearch_to_tsquery('french',$9))::float8"
        };
        let sql = format!(
            "SELECT jsonb_build_object({HIT},'similarity',NULL,'rankScore',({lexical_score})) {SCOPE} ORDER BY ({lexical_score}) {} LIMIT 64",
            if bm25 { "ASC" } else { "DESC" }
        );
        let mut hits = Vec::<SearchHit>::new();
        let mut lexical = Vec::new();
        let mut dense = Vec::new();
        let mut facts = Vec::<FactHit>::new();
        if scope.input.mode != SearchMode::Semantic {
            let rows = scoped(&sql, scope).fetch_all(&mut *tx).await?;
            for value in rows {
                let hit: SearchHit = serde_json::from_value(value)?;
                if (bm25 && hit.rank_score < 0.) ||
                    (!bm25 && hit.rank_score > 0.)
                {
                    lexical.push(hit.unit_id.clone());
                    hits.push(hit);
                }
            }
        }
        if let (Some(vector), Some(profile)) = (vector, profile) {
            ensure!(
                !vector.is_empty() &&
                    vector.len() <= 4096 &&
                    vector.iter().all(|value| value.is_finite()),
                "Invalid query vector"
            );
            let use_ann =
                diskann && [1024, 2048, 2560, 4096].contains(&vector.len());
            let distance = if use_ann {
                format!(
                    "f.ann_{0} <=> ($11::real[]::vector({0}))",
                    vector.len()
                )
            } else {
                "1-retrieval_cosine(f.embedding,$11::real[])".into()
            };
            let similarity = format!("1-({distance})");
            let condition = format!(
                " AND f.tenant_id=u.tenant_id AND f.unit_id=u.id AND f.revision=u.revision AND f.profile_id=$10 AND f.dimensions=cardinality($11::real[]){}",
                if use_ann {
                    format!(" AND f.dimensions={}", vector.len())
                } else {
                    String::new()
                }
            );
            let vector_scope = SCOPE.replacen("FROM retrieval_units u", "FROM retrieval_units u JOIN retrieval_features f ON f.tenant_id=u.tenant_id AND f.unit_id=u.id AND f.revision=u.revision",1);
            let sql = format!(
                "SELECT jsonb_build_object({HIT},'similarity',({similarity}),'rankScore',0.) {vector_scope}{condition} AND f.ordinal=-1 ORDER BY ({distance}) ASC LIMIT 64"
            );
            for value in scoped(&sql, scope)
                .bind(profile)
                .bind(vector)
                .fetch_all(&mut *tx)
                .await?
            {
                let hit: SearchHit = serde_json::from_value(value)?;
                if hit.similarity.is_none_or(|score| {
                    score < scope.input.min_similarity.unwrap_or(0.3)
                }) {
                    continue;
                }
                dense.push(hit.unit_id.clone());
                if let Some(existing) =
                    hits.iter_mut().find(|item| item.unit_id == hit.unit_id)
                {
                    existing.similarity = hit.similarity;
                } else {
                    hits.push(hit);
                }
            }
            if scope.input.mode == SearchMode::Hipporag2 {
                let sql = format!(
                    "SELECT jsonb_build_object('passage_id',u.id,'ordinal',f.ordinal,'fact',u.facts->f.ordinal,'similarity',({similarity})) {vector_scope}{condition} AND f.ordinal>=0 ORDER BY ({distance}) ASC LIMIT 64"
                );
                for value in scoped(&sql, scope)
                    .bind(profile)
                    .bind(vector)
                    .fetch_all(&mut *tx)
                    .await?
                {
                    let fact: FactHit = serde_json::from_value(value)?;
                    if fact.similarity >=
                        scope.input.min_similarity.unwrap_or(0.3)
                    {
                        facts.push(fact);
                    }
                }
            }
        }
        tx.commit().await?;
        Ok(Candidates {
            hits,
            lexical,
            dense,
            facts,
            lexical_backend: if bm25 { "bm25" } else { "postgres_text" }
                .into(),
        })
    }
}

#[cfg(test)]
mod tests {
    use anyhow::{Context, Result};
    use serde_json::json;
    use testcontainers_modules::postgres::Postgres;
    use testcontainers_modules::testcontainers::ImageExt;
    use testcontainers_modules::testcontainers::runners::AsyncRunner;

    use super::*;
    use crate::adapters::outbound::database::pengolo::PgPengoloStore;
    use crate::application::ports::pengolo_store::{Collection, PengoloStore};
    use crate::domain::document_retrieval::SearchInput;
    use crate::domain::pengolo::Record;

    #[tokio::test]
    async fn document_index_is_tenant_scoped_published_and_bound_to_live_source_content()
    -> Result<()> {
        let container = Postgres::default().with_init_sql(b"CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app'; GRANT USAGE, CREATE ON SCHEMA public TO galadril_app; CREATE EXTENSION pg_trgm;".to_vec()).with_tag("18.6-alpine").start().await?;
        let database = Database::connect(&format!(
            "postgres://galadril_app:galadril_app@{}:{}/postgres",
            container.get_host().await?,
            container.get_host_port_ipv4(5432).await?
        ))
        .await?;
        let files = PgPengoloStore::new(database.clone());
        let index = PgDocumentIndex::new(database);
        let file = Record {
            id: "a".repeat(32),
            owner_id: "alice".into(),
            project_id: None,
            expires_at: Some(now() + 60000),
            revision: 1,
            updated_at: 0,
            data: json!({"name":"unknown.txt","mime":"text/plain","checksum":"proof","status":"ready"}),
        };
        for tenant in ["tenant_a", "tenant_b"] {
            files
                .save(tenant, Collection::File, &file, false, None)
                .await?;
            index.enqueue(tenant, &file.id).await?;
        }
        let job = index
            .claim("tenant_a")
            .await?
            .context("Missing indexing job")?;
        assert!(index.claim("tenant_a").await?.is_none());
        index.enqueue("tenant_a", &file.id).await?;
        assert!(index.claim("tenant_a").await?.is_none());
        index.renew("tenant_a", &job).await?;
        let mut stale = job.clone();
        stale.lease = "f".repeat(32);
        assert!(index.renew("tenant_a", &stale).await.is_err());
        let text = "La charte prévoit une procédure en cas d’irrégularité";
        let units = vec![Unit {
            id: "b".repeat(64),
            ordinal: 0,
            text: text.into(),
            start: 0,
            end: i64::try_from(text.chars().count())?,
            page: Some(1),
            facts: Vec::new(),
        }];
        let features = vec![Feature {
            unit_id: "b".repeat(64),
            ordinal: -1,
            embedding: vec![1., 0.],
        }];
        index
            .stage("tenant_a", &job, &units, &features, Some(&"c".repeat(64)))
            .await?;
        let input = SearchInput::parse(json!({"query":"charte"}))?;
        let read = || IndexScope {
            tenant: "tenant_a",
            owner: "alice",
            input: &input,
        };
        assert!(index.candidates(read(), None, None).await?.hits.is_empty());
        index
            .publish("tenant_a", &job, Some(&"c".repeat(64)), &[])
            .await?;
        assert_eq!(
            index
                .candidates(read(), Some(&[1., 0.]), Some(&"c".repeat(64)))
                .await?
                .hits
                .len(),
            1
        );
        assert!(
            index
                .candidates(
                    IndexScope {
                        tenant: "tenant_b",
                        owner: "alice",
                        input: &input
                    },
                    None,
                    None
                )
                .await?
                .hits
                .is_empty()
        );
        assert!(
            index
                .candidates(
                    IndexScope {
                        tenant: "tenant_a",
                        owner: "bob",
                        input: &input
                    },
                    None,
                    None
                )
                .await?
                .hits
                .is_empty()
        );
        index.enqueue("tenant_a", &file.id).await?;
        let retry = index
            .claim("tenant_a")
            .await?
            .context("Missing retry job")?;
        index.stage("tenant_a", &retry, &units, &[], None).await?;
        index
            .publish(
                "tenant_a",
                &retry,
                None,
                &["embeddings_unavailable".into()],
            )
            .await?;
        assert!(!index.candidates(read(), None, None).await?.hits.is_empty());
        let mut tx = index.database.tenant("tenant_a").await?;
        sqlx::query("UPDATE retrieval_jobs SET next_attempt=0 WHERE tenant_id=$1 AND source_id=$2").bind("tenant_a").bind(&file.id).execute(&mut *tx).await?;
        tx.commit().await?;
        assert!(index.claim("tenant_a").await?.is_some());
        let mut changed = files
            .get("tenant_a", Collection::File, &file.id, false)
            .await?
            .context("Missing source")?;
        changed
            .data
            .as_object_mut()
            .context("Invalid fixture")?
            .insert("checksum".into(), json!("replacement"));
        files
            .save(
                "tenant_a",
                Collection::File,
                &changed,
                false,
                Some(changed.revision),
            )
            .await?;
        assert!(index.candidates(read(), None, None).await?.hits.is_empty());
        assert!(
            index
                .publish("tenant_a", &job, Some(&"c".repeat(64)), &[])
                .await
                .is_err()
        );
        Ok(())
    }
}
