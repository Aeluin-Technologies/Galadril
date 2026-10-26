//! Gateway retains identity, scope, IAM, and Pengolo publication authority.
use galadril_document_compiler::delegation::{
    Action, AuthorizationRequest, Grant, TemplatePolicy,
};
use galadril_document_compiler::domain::{MAX_PDF_BYTES, Scope, validate_id};
use sha2::Digest as _;

use super::*;
use crate::application::ports::access_store::AccessTuple;

pub struct CompiledArtifact<'a> {
    pub id: &'a str,
    pub name: &'a str,
    pub mime: &'a str,
    pub bytes: Vec<u8>,
}

impl PengoloService {
    /// Resolves the persisted delegation before accepting any compiler claims.
    pub async fn authorize_compiler(
        &self,
        capability: &str,
        request: AuthorizationRequest,
    ) -> Result<Grant> {
        ensure!(capability.len() == 64, "Invalid compiler delegation");
        let (tenant, generation, temporary) =
            self.store.resolve_capability(capability).await?;
        let mut context: QueryContext = serde_json::from_value(
            generation
                .data
                .get("authorization")
                .cloned()
                .context("Missing authorization context")?,
        )?;
        context.hour_utc = chrono::Timelike::hour(&chrono::Utc::now()).into();
        let actor = Principal {
            tenant: &tenant,
            user: &generation.owner_id,
            context: &context,
        };
        let chat = self
            .authorize_generation(&actor, &generation, temporary)
            .await?;
        let project = if let Some(id) = &generation.project_id {
            let project = self.project(&actor, id).await?;
            (project.data.get("contextMode").and_then(Value::as_str) !=
                Some("personal"))
            .then(|| id.clone())
        } else {
            None
        };
        let grant = Grant {
            tenant: tenant.clone(),
            actor: generation.owner_id.clone(),
            generation: generation.id.clone(),
            project,
            temporary_session: temporary.then(|| chat.id.clone()),
        };
        let Some(policy) = request.template else {
            ensure!(
                matches!(request.action, Action::Resolve | Action::PublishPdf),
                "Template policy required"
            );
            return Ok(grant);
        };
        validate_id(&policy.id, 32)?;
        if request.action == Action::Create {
            ensure!(
                !temporary &&
                    (policy.owner == actor.user ||
                        policy.scope == Scope::Plugin),
                "Template creation unavailable"
            );
            match policy.scope {
                Scope::Personal => ensure!(
                    policy.project.is_none() && policy.plugin.is_none(),
                    "Invalid personal template"
                ),
                Scope::Project => {
                    let project = self
                        .project(
                            &actor,
                            policy
                                .project
                                .as_deref()
                                .context("Project required")?,
                        )
                        .await?;
                    ensure!(
                        project.owner_id == actor.user,
                        "Project template editing requires ownership"
                    );
                },
                Scope::Tenant => {
                    ensure!(
                        self.auth
                            .is_authorized(
                                actor.user,
                                actor.tenant,
                                Permission::Manage,
                                "tenant",
                                actor.tenant,
                                Some(actor.context)
                            )
                            .await?,
                        "Tenant template administration denied"
                    );
                },
                Scope::Plugin => {
                    let plugin =
                        policy.plugin.as_deref().context("Plugin required")?;
                    ensure!(
                        self.auth
                            .is_authorized(
                                actor.user,
                                actor.tenant,
                                Permission::Use,
                                "document_plugin",
                                plugin,
                                Some(actor.context)
                            )
                            .await?,
                        "Plugin is not enabled for this tenant"
                    );
                    let catalog = self.agent.document_templates().await?;
                    ensure!(catalog.iter().any(|entry| entry.plugin_id == plugin && galadril_document_compiler::tools::plugin_template_id(actor.tenant, plugin, &entry.key) == policy.id), "Plugin template is not installed");
                },
            }
            let tuple =
                |relation: &str, subject_type: &str, subject_id: &str| {
                    AccessTuple {
                        resource_type: "document_template".into(),
                        resource_id: policy.id.clone(),
                        relation: relation.into(),
                        subject_type: subject_type.into(),
                        subject_id: subject_id.into(),
                        subject_relation: String::new(),
                    }
                };
            let mut tuples = vec![tuple("parent", "tenant", &tenant)];
            if policy.scope != Scope::Plugin {
                tuples.push(tuple("owner", "user", &grant.actor));
            }
            if let Some(project) = &policy.project {
                tuples.push(tuple("library", "project", project));
            }
            if policy.scope == Scope::Tenant {
                tuples.push(tuple("tenant_access", "tenant", &tenant));
            }
            if let Some(plugin) = &policy.plugin {
                tuples.push(tuple("plugin_source", "document_plugin", plugin));
            }
            self.access.set_many(&tenant, &tuples, true).await?;
        } else {
            let permission = match request.action {
                Action::View => Permission::View,
                Action::Use => Permission::Use,
                Action::Edit => Permission::Edit,
                Action::Publish => Permission::Publish,
                Action::Delete => Permission::Delete,
                _ => anyhow::bail!("Unsupported template action"),
            };
            ensure!(
                self.auth
                    .is_authorized(
                        actor.user,
                        actor.tenant,
                        permission,
                        "document_template",
                        &policy.id,
                        Some(actor.context)
                    )
                    .await?,
                "Template permission denied"
            );
            if !policy.owner.is_empty() {
                validate_scope(&grant, &policy)?;
                if let Some(project) = &policy.project {
                    self.project(&actor, project).await?;
                }
                if let Some(plugin) = &policy.plugin {
                    ensure!(
                        self.auth
                            .is_authorized(
                                actor.user,
                                actor.tenant,
                                Permission::Use,
                                "document_plugin",
                                plugin,
                                Some(actor.context)
                            )
                            .await?,
                        "Plugin no longer enabled"
                    );
                    ensure!(
                        self.agent
                            .document_templates()
                            .await?
                            .iter()
                            .any(|entry| entry.plugin_id == *plugin),
                        "Plugin no longer installed"
                    );
                }
            }
        }
        tracing::info!(event.name = "document_compiler.delegation.authorized");
        Ok(grant)
    }

    /// Publication uses the same reservations, checksums, retention, and
    /// promotion.
    pub async fn save_compiled_artifact(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        capability: &str,
        artifact: CompiledArtifact<'_>,
    ) -> Result<Value> {
        use sha2::{Digest, Sha256};
        let CompiledArtifact {
            id: job,
            name,
            mime,
            bytes: pdf,
        } = artifact;
        validate_id(job, 64)?;
        ensure!(pdf.len() <= MAX_PDF_BYTES, "Artifact exceeds limit");
        crate::domain::pengolo::validate_file_prefix(mime, &pdf)?;
        let extension =
            validate_upload(name, mime, i64::try_from(pdf.len())?)?;
        self.authorize_compiler(
            capability,
            AuthorizationRequest {
                action: Action::PublishPdf,
                template: None,
            },
        )
        .await?;
        let temporary = generation
            .data
            .get("input")
            .and_then(|input| input.get("temporary"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let chat = self
            .authorize_generation(actor, generation, temporary)
            .await?;
        let mut project_id = generation.project_id.clone();
        if let Some(id) = &project_id {
            let project = self.project(actor, id).await?;
            if project.data.get("contextMode").and_then(Value::as_str) ==
                Some("personal")
            {
                project_id = None;
            }
        }
        let session = temporary.then_some(chat.id);
        let digest = format!(
            "{:x}",
            Sha256::digest(
                format!(
                    "document-compiler/{}/{}/{job}",
                    actor.tenant, actor.user
                )
                .as_bytes()
            )
        );
        let id = digest
            .get(..32)
            .context("Invalid generated file ID")?
            .to_owned();
        let checksum = base64::engine::general_purpose::STANDARD
            .encode(Sha256::digest(&pdf));
        let key = if let Some(session) = &session {
            format!(
                "{}/temporary/{}/{session}/{id}.{extension}",
                actor.tenant,
                urlencoding::encode(actor.user)
            )
        } else if let Some(project) = &project_id {
            format!(
                "{}/projects/{project}/pengolo/{id}.{extension}",
                actor.tenant
            )
        } else {
            format!(
                "{}/{}/pengolo/{id}.{extension}",
                actor.tenant,
                urlencoding::encode(actor.user)
            )
        };
        let record = Record {
            id,
            owner_id: actor.user.into(),
            project_id,
            expires_at: Some(now() + STAGING_TTL_MS),
            revision: 1,
            updated_at: now(),
            data: json!({"name":name,"originalName":name,"mime":mime,"size":pdf.len(),"key":key,"stagingKey":format!("{}/{}/pengolo/{}",actor.tenant,urlencoding::encode(actor.user),digest.get(..32).context("Invalid generated ID")?),"stagingExpiresAt":now()+STAGING_TTL_MS,"checksum":checksum,"status":"uploading","temporarySession":session,"compilationJob":job,"artifactTtlMs":7_i64*24*60*60*1000}),
        };
        let file = self
            .store
            .reserve_file(actor.tenant, &record, temporary)
            .await?;
        ensure!(
            file.owner_id == actor.user &&
                file.data.get("checksum") == record.data.get("checksum") &&
                file.data.get("originalName") ==
                    record.data.get("originalName"),
            "Compilation publication conflict"
        );
        if file.data.get("status").and_then(Value::as_str) == Some("ready") {
            self.authorize_record(actor, &file).await?;
            return Ok(
                json!({"file":file_projection(file.clone()),"download":download_projection(&file,session.as_deref())}),
            );
        }
        self.objects
            .write_staging(
                &staged_file(actor.tenant, &file)?,
                axum::body::Bytes::from(pdf),
            )
            .await?;
        self.authorize_compiler(
            capability,
            AuthorizationRequest {
                action: Action::PublishPdf,
                template: None,
            },
        )
        .await?;
        self.authorize_generation(actor, generation, temporary)
            .await?;
        let file = self
            .store
            .promote_file(
                actor.tenant,
                &file,
                temporary,
                &staged_file(actor.tenant, &file)?,
                self.objects.as_ref(),
            )
            .await?;
        self.authorize_record(actor, &file).await?;
        self.store
            .record_evidence(
                actor.tenant,
                &generation.id,
                temporary,
                &[EvidenceSource {
                    resource_type: if temporary {
                        "pengolo_temporary_file"
                    } else {
                        "pengolo_file"
                    }
                    .into(),
                    resource_id: file.id.clone(),
                    manage: false,
                    entity_id: session.clone(),
                    modality: None,
                    state_type: None,
                }],
            )
            .await?;
        tracing::info!(event.name = "document_compiler.pdf.published");
        Ok(
            json!({"file":file_projection(file.clone()),"download":download_projection(&file,session.as_deref())}),
        )
    }
}

/// Personal and project content cannot be widened through a shared generation.
fn validate_scope(grant: &Grant, policy: &TemplatePolicy) -> Result<()> {
    match policy.scope {
        Scope::Personal => ensure!(
            policy.owner == grant.actor && grant.project.is_none(),
            "Personal template unavailable in this library"
        ),
        Scope::Project => ensure!(
            policy.project.is_some() && policy.project == grant.project,
            "Project template is outside the generation library"
        ),
        Scope::Tenant | Scope::Plugin => {},
    }
    Ok(())
}

static ARCHIVE_LIMIT: std::sync::OnceLock<tokio::sync::Semaphore> =
    std::sync::OnceLock::new();

impl PengoloService {
    async fn compiler_actor(
        &self,
        token: &str,
    ) -> Result<(String, Record, bool, QueryContext)> {
        let (tenant, generation, temporary) =
            self.store.resolve_capability(token).await?;
        let context = serde_json::from_value(
            generation
                .data
                .get("authorization")
                .cloned()
                .context("Missing authorization context")?,
        )?;
        Ok((tenant, generation, temporary, context))
    }

    pub async fn compiler_templates(
        &self,
        token: &str,
        compiler: &dyn crate::application::ports::document_compiler::DocumentCompiler,
        operation: galadril_document_compiler::tools::TemplateOperation,
    ) -> Result<Value> {
        use galadril_document_compiler::tools::{
            TemplateOperation, plugin_template_id,
        };
        let (tenant, generation, temporary, context) =
            self.compiler_actor(token).await?;
        let actor = Principal {
            tenant: &tenant,
            user: &generation.owner_id,
            context: &context,
        };
        self.authorize_generation(&actor, &generation, temporary)
            .await?;
        match operation {
            TemplateOperation::PluginCatalog => {
                let mut items = Vec::new();
                for item in self.agent.document_templates().await? {
                    let enabled = self
                        .auth
                        .is_authorized(
                            actor.user,
                            &tenant,
                            Permission::Use,
                            "document_plugin",
                            &item.plugin_id,
                            Some(&context),
                        )
                        .await?;
                    items.push(json!({"plugin_id":item.plugin_id,"plugin_version":item.plugin_version,"key":item.key,"name":item.template.name,"enabled":enabled}));
                }
                Ok(json!({"items":items}))
            },
            TemplateOperation::EnablePlugin { plugin_id, enabled } => {
                ensure!(
                    self.auth
                        .is_authorized(
                            actor.user,
                            &tenant,
                            Permission::Manage,
                            "tenant",
                            &tenant,
                            Some(&context)
                        )
                        .await?,
                    "Plugin administration denied"
                );
                ensure!(
                    self.agent
                        .document_templates()
                        .await?
                        .iter()
                        .any(|item| item.plugin_id == plugin_id),
                    "Plugin template inventory unavailable"
                );
                let tuple = |relation: &str| AccessTuple {
                    resource_type: "document_plugin".into(),
                    resource_id: plugin_id.clone(),
                    relation: relation.into(),
                    subject_type: "tenant".into(),
                    subject_id: tenant.clone(),
                    subject_relation: String::new(),
                };
                self.access.set(&tenant, &tuple("parent"), true).await?;
                self.access.set(&tenant, &tuple("enabled"), enabled).await?;
                Ok(json!({"enabled":enabled}))
            },
            TemplateOperation::List { after } => {
                for item in self.agent.document_templates().await? {
                    if !self
                        .auth
                        .is_authorized(
                            actor.user,
                            &tenant,
                            Permission::Use,
                            "document_plugin",
                            &item.plugin_id,
                            Some(&context),
                        )
                        .await?
                    {
                        continue;
                    }
                    let mut template = item.template;
                    template.id = Some(plugin_template_id(
                        &tenant,
                        &item.plugin_id,
                        &item.key,
                    ));
                    template.scope = Scope::Plugin;
                    template.plugin = Some(item.plugin_id);
                    template.project = None;
                    match compiler
                        .templates(token, TemplateOperation::Put { template })
                        .await
                    {
                        Ok(reference) => {
                            let id = reference
                                .get("template_id")
                                .and_then(Value::as_str)
                                .context("Invalid plugin template")?;
                            let version = reference
                                .get("version")
                                .and_then(Value::as_str)
                                .context("Invalid plugin revision")?;
                            compiler
                                .templates(
                                    token,
                                    TemplateOperation::Publish {
                                        template_id: id.into(),
                                        version: version.into(),
                                    },
                                )
                                .await?;
                        },
                        Err(_) => tracing::warn!(
                            event.name = "document_compiler.plugin.template_unavailable"
                        ),
                    }
                }
                compiler
                    .templates(token, TemplateOperation::List { after })
                    .await
            },
            TemplateOperation::Put { template } => {
                ensure!(
                    template.scope != Scope::Plugin &&
                        template.plugin.is_none(),
                    "Plugin templates must come from the installed package"
                );
                compiler
                    .templates(token, TemplateOperation::Put { template })
                    .await
            },
            TemplateOperation::Export {
                template_id,
                version,
                paths,
                name,
            } => {
                let _permit = ARCHIVE_LIMIT
                    .get_or_init(|| tokio::sync::Semaphore::new(2))
                    .acquire()
                    .await?;
                ensure!(
                    !paths.is_empty() && paths.len() <= 64,
                    "Invalid export selection"
                );
                let template =
                    compiler.template(token, &template_id, &version).await?;
                let mut archive =
                    galadril_document_compiler::archive::Archive::new();
                for path in &paths {
                    let asset = template
                        .assets
                        .iter()
                        .find(|asset| asset.path == *path)
                        .context("Export file unavailable")?;
                    archive.add(path, &asset.data)?;
                }
                let identity = format!(
                    "{:x}",
                    sha2::Sha256::digest(serde_json::to_vec(&(
                        template_id,
                        version,
                        paths
                    ))?)
                );
                self.save_compiled_artifact(
                    &actor,
                    &generation,
                    token,
                    CompiledArtifact {
                        id: &identity,
                        name: &name,
                        mime: "application/zip",
                        bytes: archive.finish()?,
                    },
                )
                .await
            },
            operation => compiler.templates(token, operation).await,
        }
    }

    pub async fn save_compiled_batch(
        &self,
        token: &str,
        compiler: &dyn crate::application::ports::document_compiler::DocumentCompiler,
        jobs: &[String],
        name: &str,
    ) -> Result<Value> {
        use sha2::Digest;
        let _permit = ARCHIVE_LIMIT
            .get_or_init(|| tokio::sync::Semaphore::new(2))
            .acquire()
            .await?;
        ensure!(
            (2..=8).contains(&jobs.len()),
            "Batch archives require two to eight documents"
        );
        let unique: std::collections::BTreeSet<&str> =
            jobs.iter().map(String::as_str).collect();
        ensure!(unique.len() == jobs.len(), "Duplicate compilation job");
        let (tenant, generation, temporary, context) =
            self.compiler_actor(token).await?;
        let actor = Principal {
            tenant: &tenant,
            user: &generation.owner_id,
            context: &context,
        };
        self.authorize_generation(&actor, &generation, temporary)
            .await?;
        let mut archive = galadril_document_compiler::archive::Archive::new();
        for (index, job) in jobs.iter().enumerate() {
            let pdf = compiler.pdf(token, job).await?;
            archive.add(&format!("document-{}.pdf", index + 1), &pdf)?;
        }
        let identity =
            format!("{:x}", sha2::Sha256::digest(serde_json::to_vec(jobs)?));
        self.save_compiled_artifact(
            &actor,
            &generation,
            token,
            CompiledArtifact {
                id: &identity,
                name,
                mime: "application/zip",
                bytes: archive.finish()?,
            },
        )
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn templates_cannot_widen_personal_or_project_content() {
        let mut grant = Grant {
            tenant: "tenant".into(),
            actor: "alice".into(),
            generation: "run".into(),
            project: None,
            temporary_session: None,
        };
        let mut policy = TemplatePolicy {
            id: "a".repeat(32),
            scope: Scope::Personal,
            owner: "alice".into(),
            project: None,
            plugin: None,
        };
        assert!(validate_scope(&grant, &policy).is_ok());
        grant.actor = "admin".into();
        assert!(validate_scope(&grant, &policy).is_err());
        grant.actor = "alice".into();
        grant.project = Some("project".into());
        assert!(validate_scope(&grant, &policy).is_err());
        policy.scope = Scope::Project;
        policy.project = Some("other".into());
        assert!(validate_scope(&grant, &policy).is_err());
        policy.project = grant.project.clone();
        assert!(validate_scope(&grant, &policy).is_ok());
    }
}
