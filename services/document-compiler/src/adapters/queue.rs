//! Typed libSQL queries keep queue persistence separate from artifact storage.
use std::path::Path;

use anyhow::{Context, Result, ensure};
use diesel::connection::SimpleConnection;
use diesel::prelude::*;
use diesel_libsql::LibSqlConnection;

diesel::table! {
    compilation_jobs (id) {
        id -> Text, tenant -> Text, actor -> Text, generation -> Text,
        fingerprint -> Text, capability -> Text, template_id -> Text, template_version -> Text, status -> Text, failure -> Text, lease -> Text,
        lease_until -> BigInt, attempts -> Integer, created_at -> BigInt,
    }
}
use self::compilation_jobs::dsl as jobs;
use crate::domain::{Job, NewJob};
#[derive(Clone, Queryable, Selectable)]
#[diesel(table_name = compilation_jobs)]
struct QueueRow {
    pub id: String,
    pub tenant: String,
    pub actor: String,
    pub generation: String,
    pub fingerprint: String,
    pub capability: String,
    pub template_id: String,
    pub template_version: String,
    pub status: String,
    pub failure: String,
    pub lease: String,
    pub lease_until: i64,
    pub attempts: i32,
    pub created_at: i64,
}

impl From<QueueRow> for Job {
    fn from(row: QueueRow) -> Self {
        Self {
            id: row.id,
            tenant: row.tenant,
            actor: row.actor,
            generation: row.generation,
            fingerprint: row.fingerprint,
            capability: row.capability,
            template_id: row.template_id,
            template_version: row.template_version,
            status: row.status,
            failure: row.failure,
            lease: row.lease,
            lease_until: row.lease_until,
            attempts: row.attempts,
            created_at: row.created_at,
        }
    }
}

pub struct Queue {
    connection: LibSqlConnection,
}

impl Queue {
    /// A local libSQL file is authoritative; replica reads cannot fence
    /// leases.
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path =
            path.as_ref().to_str().context("Queue path must be UTF-8")?;
        let mut connection = LibSqlConnection::establish(path)?;
        connection
            .batch_execute(include_str!("../../migrations/001_queue.sql"))?;
        Ok(Self { connection })
    }

    fn transaction<T>(
        &mut self,
        operation: impl FnOnce(&mut LibSqlConnection) -> Result<T>,
    ) -> Result<T> {
        self.connection.batch_execute("BEGIN IMMEDIATE")?;
        let result = operation(&mut self.connection);
        match result {
            Ok(value) => match self.connection.batch_execute("COMMIT") {
                Ok(()) => Ok(value),
                Err(error) => {
                    if self.connection.batch_execute("ROLLBACK").is_err() {
                        tracing::error!(
                            event.name =
                                "document_compiler.queue.rollback_failed"
                        );
                    }
                    Err(error.into())
                },
            },
            Err(error) => {
                if self.connection.batch_execute("ROLLBACK").is_err() {
                    tracing::error!(
                        event.name = "document_compiler.queue.rollback_failed"
                    );
                }
                Err(error)
            },
        }
    }

    pub fn expired(&mut self, now: i64) -> Result<Vec<Job>> {
        let rows = jobs::compilation_jobs
            .filter(jobs::status.eq_any(["completed", "failed", "cancelled"]))
            .filter(
                jobs::created_at
                    .le(now.saturating_sub(7 * 24 * 60 * 60 * 1000)),
            )
            .limit(32)
            .load::<QueueRow>(&mut self.connection)?;
        Ok(rows.into_iter().map(Job::from).collect())
    }

    pub fn forget(&mut self, job: &Job) -> Result<()> {
        diesel::delete(
            jobs::compilation_jobs
                .find(&job.id)
                .filter(jobs::tenant.eq(&job.tenant))
                .filter(jobs::lease.eq(&job.lease))
                .filter(jobs::status.eq_any([
                    "completed",
                    "failed",
                    "cancelled",
                ])),
        )
        .execute(&mut self.connection)?;
        Ok(())
    }

    pub fn enqueue(&mut self, item: &NewJob, now: i64) -> Result<Job> {
        self.transaction(|connection| {
            let existing = jobs::compilation_jobs
                .find(&item.id)
                .first::<QueueRow>(connection)
                .optional()?;
            if let Some(existing) = existing {
                ensure!(
                    existing.tenant == item.tenant &&
                        existing.actor == item.actor &&
                        existing.generation == item.generation &&
                        existing.fingerprint == item.fingerprint,
                    "Idempotency conflict"
                );
                return Ok(existing.into());
            }
            let active = jobs::status.eq_any(["queued", "running"]);
            let count: i64 = jobs::compilation_jobs
                .filter(active)
                .count()
                .get_result(connection)?;
            let tenant_count: i64 = jobs::compilation_jobs
                .filter(jobs::tenant.eq(&item.tenant))
                .filter(jobs::status.eq_any(["queued", "running"]))
                .count()
                .get_result(connection)?;
            ensure!(
                count < 256 && tenant_count < 32,
                "Compilation queue is full"
            );
            diesel::insert_into(jobs::compilation_jobs)
                .values((
                    jobs::id.eq(&item.id),
                    jobs::tenant.eq(&item.tenant),
                    jobs::actor.eq(&item.actor),
                    jobs::generation.eq(&item.generation),
                    jobs::fingerprint.eq(&item.fingerprint),
                    jobs::capability.eq(&item.capability),
                    jobs::template_id.eq(&item.template_id),
                    jobs::template_version.eq(&item.template_version),
                    jobs::status.eq("queued"),
                    jobs::created_at.eq(now),
                ))
                .execute(connection)?;
            Ok(jobs::compilation_jobs
                .find(&item.id)
                .first::<QueueRow>(connection)?
                .into())
        })
    }

    pub fn get(&mut self, tenant: &str, id: &str) -> Result<Job> {
        Ok(jobs::compilation_jobs
            .find(id)
            .filter(jobs::tenant.eq(tenant))
            .first::<QueueRow>(&mut self.connection)?
            .into())
    }

    /// Compare-and-swap remains the final fence even with another controller.
    pub fn claim(&mut self, now: i64) -> Result<Option<Job>> {
        self.transaction(|connection| {
            let eligible = jobs::status
                .eq("queued")
                .or(jobs::status.eq("running").and(jobs::lease_until.lt(now)));
            let candidate = jobs::compilation_jobs
                .filter(eligible)
                .order((jobs::created_at.asc(), jobs::id.asc()))
                .first::<QueueRow>(connection)
                .optional()?;
            let Some(candidate) = candidate else {
                return Ok(None);
            };
            if candidate.attempts >= 3 {
                diesel::update(
                    jobs::compilation_jobs
                        .find(&candidate.id)
                        .filter(jobs::lease.eq(&candidate.lease)),
                )
                .set((
                    jobs::status.eq("failed"),
                    jobs::failure.eq("retry_limit"),
                ))
                .execute(connection)?;
                return Ok(None);
            }
            let lease = uuid::Uuid::new_v4().simple().to_string();
            let changed = diesel::update(
                jobs::compilation_jobs
                    .find(&candidate.id)
                    .filter(jobs::lease.eq(&candidate.lease))
                    .filter(jobs::status.eq(&candidate.status)),
            )
            .set((
                jobs::status.eq("running"),
                jobs::lease.eq(&lease),
                jobs::lease_until.eq(now + 90_000),
                jobs::attempts.eq(candidate.attempts + 1),
            ))
            .execute(connection)?;
            if changed == 0 {
                return Ok(None);
            }
            Ok(Some(
                jobs::compilation_jobs
                    .find(&candidate.id)
                    .first::<QueueRow>(connection)?
                    .into(),
            ))
        })
    }

    /// Cancellation and superseded leases cannot be overwritten by late
    /// results.
    pub fn finish(
        &mut self,
        job: &Job,
        status: &str,
        failure: &str,
    ) -> Result<bool> {
        ensure!(
            matches!(status, "completed" | "failed"),
            "Invalid terminal state"
        );
        Ok(diesel::update(
            jobs::compilation_jobs
                .find(&job.id)
                .filter(jobs::tenant.eq(&job.tenant))
                .filter(jobs::lease.eq(&job.lease))
                .filter(jobs::status.eq("running")),
        )
        .set((jobs::status.eq(status), jobs::failure.eq(failure)))
        .execute(&mut self.connection)? ==
            1)
    }

    pub fn cancel(&mut self, tenant: &str, id: &str) -> Result<Job> {
        diesel::update(
            jobs::compilation_jobs
                .find(id)
                .filter(jobs::tenant.eq(tenant))
                .filter(jobs::status.eq_any(["queued", "running"])),
        )
        .set(jobs::status.eq("cancelled"))
        .execute(&mut self.connection)?;
        self.get(tenant, id)
    }
}

#[derive(Clone)]
pub struct QueueHandle(std::sync::Arc<std::sync::Mutex<Queue>>);
impl QueueHandle {
    pub fn new(queue: Queue) -> Self {
        Self(std::sync::Arc::new(std::sync::Mutex::new(queue)))
    }

    async fn run<T: Send + 'static>(
        &self,
        operation: impl FnOnce(&mut Queue) -> Result<T> + Send + 'static,
    ) -> Result<T> {
        let queue = std::sync::Arc::clone(&self.0);
        tokio::task::spawn_blocking(move || {
            let mut queue = queue
                .lock()
                .map_err(|_| anyhow::anyhow!("Queue unavailable"))?;
            operation(&mut queue)
        })
        .await?
    }
}
#[async_trait::async_trait]
impl crate::ports::JobQueue for QueueHandle {
    async fn expired(&self, now: i64) -> Result<Vec<Job>> {
        self.run(move |queue| queue.expired(now)).await
    }

    async fn forget(&self, job: Job) -> Result<()> {
        self.run(move |queue| queue.forget(&job)).await
    }

    async fn enqueue(&self, item: NewJob, now: i64) -> Result<Job> {
        self.run(move |queue| queue.enqueue(&item, now)).await
    }

    async fn claim(&self, now: i64) -> Result<Option<Job>> {
        self.run(move |queue| queue.claim(now)).await
    }

    async fn get(&self, tenant: &str, id: &str) -> Result<Job> {
        let tenant = tenant.to_owned();
        let id = id.to_owned();
        self.run(move |queue| queue.get(&tenant, &id)).await
    }

    async fn cancel(&self, tenant: &str, id: &str) -> Result<Job> {
        let tenant = tenant.to_owned();
        let id = id.to_owned();
        self.run(move |queue| queue.cancel(&tenant, &id)).await
    }

    async fn finish(&self, job: Job, succeeded: bool) -> Result<bool> {
        self.run(move |queue| {
            queue.finish(
                &job,
                if succeeded { "completed" } else { "failed" },
                if succeeded { "" } else { "compilation_failed" },
            )
        })
        .await
    }
}
