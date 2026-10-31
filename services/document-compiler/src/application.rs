//! Durable orchestration never treats workload identity as tenant authority.
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use galadril_document_compiler::delegation::{
    Action, AuthorizationRequest, Grant, TemplatePolicy,
};
use galadril_document_compiler::domain::{
    Document, MAX_PDF_BYTES, MAX_PROJECT_BYTES, Template, validate_id,
};
use galadril_document_compiler::proto;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::domain::{Job, NewJob};
use crate::ports::{Authorizer, JobQueue, ObjectStore, Sandbox};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Submission {
    grant: Grant,
    template_id: String,
    version: String,
    document: Document,
}

pub struct Compiler {
    pub queue: Arc<dyn JobQueue>,
    objects: Arc<dyn ObjectStore>,
    auth: Arc<dyn Authorizer>,
    sandbox: Arc<dyn Sandbox>,
}

fn root(tenant: &str) -> String {
    format!("{tenant}/document-compiler")
}
fn template_key(tenant: &str, id: &str, version: &str) -> Result<String> {
    validate_id(id, 32)?;
    validate_id(version, 64)?;
    Ok(format!("{}/templates/{id}/{version}.json", root(tenant)))
}
fn deleted_key(tenant: &str, id: &str) -> String {
    format!("{}/deleted/{id}", root(tenant))
}
fn published_key(tenant: &str, id: &str, version: &str) -> String {
    format!("{}/published/{id}/{version}", root(tenant))
}
fn job_key(tenant: &str, id: &str, extension: &str) -> String {
    format!("{}/jobs/{id}.{extension}", root(tenant))
}

pub fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|time| time.as_millis().min(i64::MAX as u128) as i64)
        .unwrap_or(0)
}

impl Compiler {
    pub fn new(
        queue: Arc<dyn JobQueue>,
        objects: Arc<dyn ObjectStore>,
        auth: Arc<dyn Authorizer>,
        sandbox: Arc<dyn Sandbox>,
    ) -> Self {
        Self {
            queue,
            objects,
            auth,
            sandbox,
        }
    }

    pub async fn resolve(&self, capability: &str) -> Result<Grant> {
        self.auth
            .authorize(
                capability,
                AuthorizationRequest {
                    action: Action::Resolve,
                    template: None,
                },
            )
            .await
    }

    async fn authorize(
        &self,
        capability: &str,
        grant: &Grant,
        action: Action,
        policy: TemplatePolicy,
    ) -> Result<()> {
        let current = self
            .auth
            .authorize(
                capability,
                AuthorizationRequest {
                    action,
                    template: Some(policy),
                },
            )
            .await?;
        ensure!(&current == grant, "Delegation changed");
        Ok(())
    }

    /// Structural authorization precedes fetching the manifest and every
    /// asset.
    async fn template(
        &self,
        capability: &str,
        grant: &Grant,
        id: &str,
        version: &str,
        action: Action,
    ) -> Result<Template> {
        let key = template_key(&grant.tenant, id, version)?;
        self.authorize(
            capability,
            grant,
            action,
            TemplatePolicy {
                id: id.into(),
                scope: galadril_document_compiler::domain::Scope::Tenant,
                owner: String::new(),
                project: None,
                plugin: None,
            },
        )
        .await?;
        ensure!(
            !self.objects.exists(&deleted_key(&grant.tenant, id)).await?,
            "Template unavailable"
        );
        let mut template: Template = serde_json::from_slice(
            &self.objects.get(&key, MAX_PROJECT_BYTES * 5).await?,
        )?;
        ensure!(
            template.id == id && template.version == version,
            "Template identity mismatch"
        );
        template.seal()?;
        ensure!(template.version == version, "Template digest mismatch");
        self.authorize(capability, grant, action, (&template).into())
            .await?;
        Ok(template)
    }

    pub async fn read_template(
        &self,
        capability: &str,
        reference: proto::TemplateRef,
    ) -> Result<Template> {
        let grant = self.resolve(capability).await?;
        self.template(
            capability,
            &grant,
            &reference.template_id,
            &reference.version,
            Action::View,
        )
        .await
    }

    pub async fn list_templates(
        &self,
        capability: &str,
        after: &str,
    ) -> Result<(Vec<Template>, String)> {
        let grant = self.resolve(capability).await?;
        let prefix = format!("{}/templates/", root(&grant.tenant));
        let start_after = if after.is_empty() {
            String::new()
        } else {
            ensure!(after.len() == 96, "Invalid template cursor");
            let id = after.get(..32).context("Invalid template cursor")?;
            let version =
                after.get(32..).context("Invalid template cursor")?;
            template_key(&grant.tenant, id, version)?
        };
        let (keys, cursor) = self.objects.list(&prefix, &start_after).await?;
        let cursor = if cursor.is_empty() {
            String::new()
        } else {
            let (id, version) = cursor
                .strip_prefix(&prefix)
                .and_then(|value| value.strip_suffix(".json"))
                .and_then(|value| value.split_once('/'))
                .context("Invalid catalog cursor")?;
            validate_id(id, 32)?;
            validate_id(version, 64)?;
            format!("{id}{version}")
        };
        let mut templates = Vec::new();
        for key in keys {
            let Some((id, version)) = key
                .strip_prefix(&prefix)
                .and_then(|s| s.strip_suffix(".json"))
                .and_then(|s| s.split_once('/'))
            else {
                continue;
            };
            match self
                .template(capability, &grant, id, version, Action::View)
                .await
            {
                Ok(mut template) => {
                    template.assets.clear();
                    templates.push(template);
                },
                Err(_) => tracing::debug!(
                    event.name = "document_compiler.catalog.filtered"
                ),
            }
        }
        Ok((templates, cursor))
    }

    /// New revisions cannot change a template's existing ownership or scope.
    pub async fn put_template(
        &self,
        capability: &str,
        mut template: Template,
    ) -> Result<proto::TemplateRef> {
        let grant = self.resolve(capability).await?;
        let creating = template.id.is_empty();
        if creating {
            template.id = format!(
                "0{}",
                uuid::Uuid::new_v4()
                    .simple()
                    .to_string()
                    .get(..31)
                    .context("Invalid generated ID")?
            );
        }
        if template.scope == galadril_document_compiler::domain::Scope::Plugin
        {
            template.owner = format!(
                "plugin:{}",
                template.plugin.as_deref().context("Plugin required")?
            );
        } else if creating {
            template.owner = grant.actor.clone();
        } else {
            validate_id(&template.id, 32)?;
        }
        validate_id(&template.id, 32)?;
        let prefix =
            format!("{}/templates/{}/", root(&grant.tenant), template.id);
        let (previous, _) = self.objects.list(&prefix, "").await?;
        if let Some(previous) = previous.first() {
            let version = previous
                .strip_prefix(&prefix)
                .and_then(|s| s.strip_suffix(".json"))
                .context("Invalid prior revision")?;
            let action = if template.scope ==
                galadril_document_compiler::domain::Scope::Plugin
            {
                Action::View
            } else {
                Action::Edit
            };
            let existing = self
                .template(capability, &grant, &template.id, version, action)
                .await?;
            ensure!(
                existing.scope == template.scope &&
                    existing.project == template.project &&
                    existing.plugin == template.plugin,
                "Template scope is immutable"
            );
            template.owner = existing.owner;
        } else {
            ensure!(
                creating ||
                    (template.scope ==
                        galadril_document_compiler::domain::Scope::Plugin &&
                        template.id.starts_with('f')),
                "Template does not exist"
            );
            template.validate()?;
            self.authorize(
                capability,
                &grant,
                Action::Create,
                (&template).into(),
            )
            .await?;
        }
        template.seal()?;
        ensure!(
            !self
                .objects
                .exists(&deleted_key(&grant.tenant, &template.id))
                .await?,
            "Template deleted"
        );
        self.objects
            .put_new(
                &template_key(&grant.tenant, &template.id, &template.version)?,
                serde_json::to_vec(&template)?,
            )
            .await?;
        tracing::info!(
            event.name = "document_compiler.template.version_created"
        );
        Ok(proto::TemplateRef {
            template_id: template.id,
            version: template.version,
        })
    }

    pub async fn mutate_template(
        &self,
        capability: &str,
        reference: proto::TemplateRef,
        action: Action,
    ) -> Result<()> {
        ensure!(
            matches!(action, Action::Publish | Action::Delete),
            "Invalid mutation"
        );
        let grant = self.resolve(capability).await?;
        self.template(
            capability,
            &grant,
            &reference.template_id,
            &reference.version,
            action,
        )
        .await?;
        let key = if action == Action::Delete {
            deleted_key(&grant.tenant, &reference.template_id)
        } else {
            published_key(
                &grant.tenant,
                &reference.template_id,
                &reference.version,
            )
        };
        self.objects.put_new(&key, Vec::new()).await
    }

    pub async fn submit(
        &self,
        capability: &str,
        request: proto::CompileRequest,
    ) -> Result<proto::Job> {
        let grant = self.resolve(capability).await?;
        ensure!(
            uuid::Uuid::parse_str(&request.idempotency_key).is_ok(),
            "Invalid idempotency key"
        );
        ensure!(
            request.document_json.len() <= MAX_PROJECT_BYTES * 2,
            "Document exceeds limit"
        );
        let reference = request.template.context("Template is required")?;
        let template = self
            .template(
                capability,
                &grant,
                &reference.template_id,
                &reference.version,
                Action::Use,
            )
            .await?;
        let published =
            published_key(&grant.tenant, &template.id, &template.version);
        if !self.objects.exists(&published).await? {
            self.mutate_template(
                capability,
                reference.clone(),
                Action::Publish,
            )
            .await?;
            tracing::info!(
                event.name =
                    "document_compiler.template.published_for_compile"
            );
        }
        ensure!(
            self.objects.exists(&published).await?,
            "Template publication unavailable"
        );
        let document: Document =
            serde_json::from_slice(&request.document_json)?;
        template.render(&document)?;
        let id = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&(
                &grant.tenant,
                &grant.actor,
                &grant.generation,
                &request.idempotency_key
            ))?)
        );
        let submission = Submission {
            grant: grant.clone(),
            template_id: template.id,
            version: template.version,
            document,
        };
        let bytes = serde_json::to_vec(&submission)?;
        let fingerprint = format!("{:x}", Sha256::digest(&bytes));
        self.objects
            .put_new(&job_key(&grant.tenant, &id, "json"), bytes)
            .await?;
        let item = NewJob {
            id,
            tenant: grant.tenant,
            actor: grant.actor,
            generation: grant.generation,
            fingerprint,
            capability: capability.into(),
            template_id: submission.template_id,
            template_version: submission.version,
        };
        let job = self.queue.enqueue(item, now()).await?;
        Ok(projection(job))
    }

    async fn job(&self, capability: &str, id: &str) -> Result<(Grant, Job)> {
        validate_id(id, 64)?;
        let grant = self.resolve(capability).await?;
        let tenant = grant.tenant.clone();
        let id = id.to_owned();
        let job = self.queue.get(&tenant, &id).await?;
        ensure!(
            job.actor == grant.actor && job.generation == grant.generation,
            "Job unavailable"
        );
        Ok((grant, job))
    }

    pub async fn get_job(
        &self,
        capability: &str,
        id: &str,
        cancel: bool,
    ) -> Result<proto::Job> {
        let (grant, job) = self.job(capability, id).await?;
        let job = if cancel {
            self.queue.cancel(&grant.tenant, &job.id).await?
        } else {
            job
        };
        Ok(projection(job))
    }

    pub async fn pdf(&self, capability: &str, id: &str) -> Result<Vec<u8>> {
        let (grant, job) = self.job(capability, id).await?;
        ensure!(
            job.status == "completed" &&
                now() <
                    job.created_at
                        .saturating_add(7 * 24 * 60 * 60 * 1000),
            "Compilation is incomplete or expired"
        );
        self.template(
            capability,
            &grant,
            &job.template_id,
            &job.template_version,
            Action::Use,
        )
        .await?;
        self.auth
            .authorize(
                capability,
                AuthorizationRequest {
                    action: Action::PublishPdf,
                    template: None,
                },
            )
            .await?;
        self.objects
            .get(&job_key(&grant.tenant, id, "pdf"), MAX_PDF_BYTES)
            .await
    }

    #[tracing::instrument(skip_all, name = "document_compiler.execute")]
    pub async fn execute(&self, job: &Job) -> Result<()> {
        let authorized = self.resolve(&job.capability).await?;
        ensure!(
            authorized.tenant == job.tenant &&
                authorized.actor == job.actor &&
                authorized.generation == job.generation,
            "Queued authority changed"
        );
        let template = self
            .template(
                &job.capability,
                &authorized,
                &job.template_id,
                &job.template_version,
                Action::Use,
            )
            .await?;
        let bytes = self
            .objects
            .get(
                &job_key(&job.tenant, &job.id, "json"),
                MAX_PROJECT_BYTES * 5,
            )
            .await?;
        ensure!(
            format!("{:x}", Sha256::digest(&bytes)) == job.fingerprint,
            "Queued input changed"
        );
        let submission: Submission = serde_json::from_slice(&bytes)?;
        let grant = self.resolve(&job.capability).await?;
        ensure!(
            grant == submission.grant &&
                grant.tenant == job.tenant &&
                grant.actor == job.actor &&
                grant.generation == job.generation,
            "Queued delegation changed"
        );
        ensure!(
            template.id == submission.template_id &&
                template.version == submission.version,
            "Queued template changed"
        );
        let compile =
            self.sandbox.compile(template.render(&submission.document)?);
        tokio::pin!(compile);
        let mut interval =
            tokio::time::interval(std::time::Duration::from_millis(500));
        let mut last_authorized = std::time::Instant::now();
        let pdf = loop {
            tokio::select! {
                result = &mut compile => break result?,
                _ = interval.tick() => {
                    let id = job.id.clone(); let tenant = job.tenant.clone();
                    let current = self.queue.get(&tenant, &id).await?;
                    ensure!(current.status == "running" && current.lease == job.lease, "Compilation cancelled");
                    if last_authorized.elapsed() >= std::time::Duration::from_secs(2) {
                        self.authorize(&job.capability, &grant, Action::Use, (&template).into()).await?;
                        last_authorized = std::time::Instant::now();
                    }
                },
            }
        };
        ensure!(
            pdf.starts_with(b"%PDF-") && pdf.len() <= MAX_PDF_BYTES,
            "Invalid compiler output"
        );
        self.template(
            &job.capability,
            &grant,
            &submission.template_id,
            &submission.version,
            Action::Use,
        )
        .await?;
        self.objects
            .put_new(&job_key(&job.tenant, &job.id, "pdf"), pdf)
            .await?;
        Ok(())
    }

    async fn cleanup(&self) -> Result<()> {
        for job in self.queue.expired(now()).await? {
            for extension in ["json", "pdf"] {
                self.objects
                    .delete(&job_key(&job.tenant, &job.id, extension))
                    .await?;
            }
            self.queue.forget(job).await?;
        }
        Ok(())
    }

    /// Fixed concurrency bounds apply equally to fresh and recovered work.
    pub async fn run(
        self: Arc<Self>,
        mut shutdown: tokio::sync::watch::Receiver<bool>,
    ) -> Result<()> {
        let mut tasks = tokio::task::JoinSet::new();
        let mut cleanup_tasks = tokio::task::JoinSet::new();
        let mut cleanup_interval =
            tokio::time::interval(std::time::Duration::from_secs(60));
        let mut interval =
            tokio::time::interval(std::time::Duration::from_millis(250));
        loop {
            tokio::select! {
                _ = cleanup_interval.tick(), if cleanup_tasks.is_empty() => {
                    let service = Arc::clone(&self);
                    cleanup_tasks.spawn(async move { if service.cleanup().await.is_err() { tracing::warn!(event.name = "document_compiler.artifacts.cleanup_deferred"); } });
                },
                result = cleanup_tasks.join_next(), if !cleanup_tasks.is_empty() => { if let Some(Err(_)) = result { tracing::error!(event.name = "document_compiler.artifacts.cleanup_failed"); } },
                changed = shutdown.changed() => { changed?; if *shutdown.borrow() { break; } },
                result = tasks.join_next(), if !tasks.is_empty() => { if let Some(Err(_)) = result { tracing::error!(event.name = "document_compiler.task.failed"); } },
                _ = interval.tick(), if tasks.len() < 2 => {
                    if let Some(job) = self.queue.claim(now()).await? {
                        let service = Arc::clone(&self);
                        tasks.spawn(async move {
                            let started = std::time::Instant::now();
                            let result = tokio::time::timeout(std::time::Duration::from_secs(75), service.execute(&job)).await;
                            let succeeded = matches!(result, Ok(Ok(())));
                            let meter = opentelemetry::global::meter("galadril.document_compiler");
                            meter.u64_counter("compilation.count").build().add(1, &[opentelemetry::KeyValue::new("succeeded", succeeded)]);
                            meter.f64_histogram("compilation.duration").with_unit("s").build().record(started.elapsed().as_secs_f64(), &[]);
                            match service.queue.finish(job, succeeded).await {
                                Ok(_) => tracing::info!(event.name = "document_compiler.job.finished", succeeded),
                                Err(_) => tracing::error!(event.name = "document_compiler.queue.finish_failed"),
                            }
                        });
                    }
                },
            }
        }
        cleanup_tasks.abort_all();
        while cleanup_tasks.join_next().await.is_some() {}
        tasks.abort_all();
        while let Some(result) = tasks.join_next().await {
            if result.is_err() {
                tracing::debug!(
                    event.name = "document_compiler.shutdown.cancelled"
                );
            }
        }
        Ok(())
    }
}

fn projection(job: Job) -> proto::Job {
    proto::Job {
        job_id: job.id,
        status: job.status,
        failure_code: job.failure,
    }
}
