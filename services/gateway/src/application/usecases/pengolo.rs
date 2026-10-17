//! Workspace authorization remains local while Scribe owns detached inference.

use std::hash::{Hash, Hasher};
use std::sync::{Arc, OnceLock};

use anyhow::{Context, Result, ensure};
use base64::Engine as _;
use chrono::Timelike as _;
use futures::StreamExt as _;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::application::ports::access_store::AccessStore;
use crate::application::ports::conversation_agent::{
    AgentModelCatalog, ConversationAgent,
};
use crate::application::ports::conversation_store::EvidenceSource;
use crate::application::ports::iam_store::IamStore;
use crate::application::ports::pengolo_store::{
    Collection, PengoloObjects, PengoloStore, RecordQuery, StagedFile,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::identity::IdentityService;
use crate::domain::pengolo::{
    OutputEvent, ProjectInput, Record, STAGING_TTL_MS, SendInput,
    document_scope_allows, file_available, validate_upload,
};

pub struct Principal<'a> {
    pub tenant: &'a str,
    pub user: &'a str,
    pub context: &'a QueryContext,
}

pub struct PengoloService {
    pub store: Arc<dyn PengoloStore>,
    agent: Arc<dyn ConversationAgent>,
    objects: Arc<dyn PengoloObjects>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    iam: Arc<dyn IamStore>,
    access: Arc<dyn AccessStore>,
    signals: WorkspaceSignals,
}

struct WorkspaceSignals {
    output: tokio::sync::broadcast::Sender<Arc<str>>,
    identity: tokio::sync::watch::Sender<u64>,
}
impl WorkspaceSignals {
    fn new() -> Self {
        Self {
            output: tokio::sync::broadcast::channel(256).0,
            identity: tokio::sync::watch::channel(0).0,
        }
    }
}

/// Avoids unrelated database replays while retaining reconciliation across
/// replicas.
pub async fn wait_for_output(
    changes: &mut tokio::sync::broadcast::Receiver<Arc<str>>,
    generation_id: &str,
) {
    let deadline = tokio::time::sleep(std::time::Duration::from_secs(2));
    tokio::pin!(deadline);
    loop {
        tokio::select! {
            event = changes.recv() => {
                match event {
                    Ok(id) if id.as_ref() != generation_id => {},
                    _ => break,
                }
            }
            () = &mut deadline => break,
        }
    }
}

fn workspace_version(
    tenant: &str,
    user: &str,
    roles: &[String],
    projects: &[Record],
    mut denied_owned: Vec<String>,
) -> String {
    let mut projects = projects
        .iter()
        .filter(|project| project.owner_id != user)
        .map(|project| (project.id.as_str(), project.revision))
        .collect::<Vec<_>>();
    projects.sort_unstable();
    denied_owned.sort_unstable();
    let mut hash = std::collections::hash_map::DefaultHasher::new();
    (tenant, user, roles, projects, denied_owned).hash(&mut hash);
    format!("{:016x}", hash.finish())
}

fn replay_projection(
    chat: &Record,
    generation_id: &str,
    status: &str,
    events: &[OutputEvent],
) -> Value {
    let status = events
        .last()
        .filter(|event| {
            matches!(event.kind.as_str(), "completed" | "failed" | "cancelled")
        })
        .map_or(status, |event| event.kind.as_str());
    let terminal = matches!(status, "completed" | "failed" | "cancelled");
    let message = if terminal {
        chat.data
            .get("messages")
            .and_then(Value::as_array)
            .and_then(|messages| {
                messages.iter().find(|message| {
                    message.get("role").and_then(Value::as_str) ==
                        Some("assistant") &&
                        message.get("generationId").and_then(Value::as_str) ==
                            Some(generation_id)
                })
            })
    } else {
        None
    };
    json!({"events":events,"status":status,"generationId":generation_id,"message":message,"chatRevision":if terminal {Some(chat.revision)} else {None},"chatUpdatedAt":if terminal {Some(chat.updated_at)} else {None}})
}

static OUTBOUND_CLIENT: OnceLock<
    std::result::Result<reqwest::Client, reqwest::Error>,
> = OnceLock::new();
fn outbound_client() -> Result<&'static reqwest::Client> {
    OUTBOUND_CLIENT
        .get_or_init(|| {
            reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(std::time::Duration::from_secs(15))
                .build()
        })
        .as_ref()
        .map_err(|_| anyhow::anyhow!("Outbound transport unavailable"))
}
fn file_projection(mut record: Record) -> Record {
    if let Some(data) = record.data.as_object_mut() {
        data.remove("key");
        data.remove("stagingKey");
    }
    record
}
fn staged_file<'a>(
    tenant: &'a str,
    file: &'a Record,
) -> Result<StagedFile<'a>> {
    Ok(StagedFile {
        tenant,
        owner: &file.owner_id,
        source: string(&file.data, "stagingKey")?,
        destination: string(&file.data, "key")?,
        mime: string(&file.data, "mime")?,
        size: file
            .data
            .get("size")
            .and_then(Value::as_i64)
            .context("Invalid file size")?,
        name: string(&file.data, "name")?,
        checksum: string(&file.data, "checksum")?,
    })
}

fn file_url(id: &str, session: Option<&str>) -> String {
    format!(
        "/pengolo/files/{id}{}",
        session
            .map(|session| format!("?session={session}"))
            .unwrap_or_default()
    )
}

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .context("Invalid workspace metadata")
}
fn parse<T: DeserializeOwned>(value: Value) -> Result<T> {
    serde_json::from_value(value)
        .map_err(|_| anyhow::anyhow!("Invalid Pengolo request"))
}
fn project_visible(project: &Record, user: &str, roles: &[String]) -> bool {
    project.owner_id == user ||
        (project.data.get("shared").and_then(Value::as_bool) == Some(true) &&
            project
                .data
                .get("roles")
                .and_then(Value::as_array)
                .is_some_and(|allowed| {
                    allowed
                        .iter()
                        .filter_map(Value::as_str)
                        .any(|role| roles.iter().any(|owned| owned == role))
                }))
}
fn generation_projection(record: &Record) -> Record {
    Record {
        data: json!({"chatId":record.data.get("chatId"),"status":record.data.get("status"),"sequence":record.data.get("sequence")}),
        ..record.clone()
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct UploadInput {
    name: String,
    mime: String,
    size: i64,
    checksum: String,
    project_id: Option<String>,
    temporary_session: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct FileInput {
    id: String,
    temporary_session: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct ChatInput {
    id: String,
    revision: i64,
    title: Option<String>,
    pinned: Option<bool>,
    project_id: Option<String>,
    #[serde(default)]
    move_project: bool,
    message_id: Option<String>,
    task_checks: Option<Vec<TaskCheck>>,
    #[serde(default)]
    temporary: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TaskCheck {
    index: i32,
    checked: bool,
}

fn save_task_checks(
    data: &mut Value,
    id: &str,
    checks: &[TaskCheck],
) -> Result<()> {
    ensure!(checks.len() <= 256, "Too many task selections");
    let mut seen = std::collections::HashSet::with_capacity(checks.len());
    ensure!(
        checks.iter().all(|check| (0..256).contains(&check.index) &&
            seen.insert(check.index)),
        "Invalid task selection"
    );
    let message = data
        .get_mut("messages")
        .and_then(Value::as_array_mut)
        .and_then(|messages| {
            messages.iter_mut().find(|message| {
                message.get("id").and_then(Value::as_str) == Some(id)
            })
        })
        .context("Message unavailable")?;
    ensure!(
        message.get("role").and_then(Value::as_str) == Some("assistant") &&
            matches!(
                message.get("status").and_then(Value::as_str),
                Some("completed" | "failed" | "cancelled")
            ),
        "Message cannot be edited"
    );
    message
        .as_object_mut()
        .context("Invalid message")?
        .insert("taskChecks".into(), serde_json::to_value(checks)?);
    Ok(())
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct CancelInput {
    id: String,
    #[serde(default)]
    temporary: bool,
}

impl PengoloService {
    pub fn new(
        store: Arc<dyn PengoloStore>,
        agent: Arc<dyn ConversationAgent>,
        objects: Arc<dyn PengoloObjects>,
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        iam: Arc<dyn IamStore>,
        access: Arc<dyn AccessStore>,
    ) -> Self {
        Self {
            store,
            agent,
            objects,
            identity,
            auth,
            iam,
            access,
            signals: WorkspaceSignals::new(),
        }
    }

    pub fn output_changes(
        &self,
    ) -> tokio::sync::broadcast::Receiver<Arc<str>> {
        self.signals.output.subscribe()
    }

    pub fn identity_changes(&self) -> tokio::sync::watch::Receiver<u64> {
        self.signals.identity.subscribe()
    }

    pub fn invalidate_identity(&self) {
        self.signals
            .identity
            .send_modify(|version| *version = version.wrapping_add(1));
    }

    fn notify_output(&self, id: &str) {
        if self.signals.output.receiver_count() > 0 &&
            self.signals.output.send(Arc::from(id)).is_err()
        {
            tracing::debug!(
                event.name = "pengolo.output.subscriber_disconnected"
            );
        }
    }

    pub async fn verify(&self, actor: &Principal<'_>) -> Result<()> {
        self.identity.verify_user(actor.tenant, actor.user).await?;
        ensure!(
            self.auth
                .is_authorized(
                    actor.user,
                    actor.tenant,
                    Permission::View,
                    "tenant",
                    actor.tenant,
                    Some(actor.context)
                )
                .await?,
            "Pengolo access denied"
        );
        Ok(())
    }

    pub async fn identity(&self, actor: &Principal<'_>) -> Result<Value> {
        self.verify(actor).await?;
        let mut roles = self
            .iam
            .effective_role_names_for_user(actor.tenant, actor.user)
            .await?;
        roles.sort();
        let (projects, denied_owned) = self.project_access(actor).await?;
        let administrator = self
            .auth
            .is_authorized(
                actor.user,
                actor.tenant,
                Permission::Manage,
                "tenant",
                actor.tenant,
                Some(actor.context),
            )
            .await?;
        let catalog = self.models(actor).await?;
        let web_search_available = catalog
            .models
            .iter()
            .any(|model| model.provider == "openai") ||
            std::env::var("PENGOLO_WEB_SEARCH_URL").is_ok();
        Ok(
            json!({"version":workspace_version(actor.tenant,actor.user,&roles,&projects,denied_owned),"webSearchAvailable":web_search_available,"administrator":administrator}),
        )
    }

    pub async fn models(
        &self,
        actor: &Principal<'_>,
    ) -> Result<AgentModelCatalog> {
        self.verify(actor).await?;
        let mut catalog = self.agent.models().await?;
        catalog.models.retain(|model| {
            model
                .reasoning_efforts
                .iter()
                .any(|effort| effort == "medium")
        });
        if !catalog
            .models
            .iter()
            .any(|model| Some(&model.alias) == catalog.default_model.as_ref())
        {
            catalog.default_model =
                catalog.models.first().map(|model| model.alias.clone());
        }
        Ok(catalog)
    }

    pub async fn project(
        &self,
        actor: &Principal<'_>,
        id: &str,
    ) -> Result<Record> {
        self.verify(actor).await?;
        let project = self
            .store
            .get(actor.tenant, Collection::Project, id, false)
            .await?
            .context("Project unavailable")?;
        let roles = self
            .iam
            .effective_role_names_for_user(actor.tenant, actor.user)
            .await?;
        ensure!(
            project_visible(&project, actor.user, &roles),
            "Project unavailable"
        );
        ensure!(
            self.auth
                .is_authorized(
                    actor.user,
                    actor.tenant,
                    Permission::View,
                    "project",
                    id,
                    Some(actor.context)
                )
                .await?,
            "Project unavailable"
        );
        Ok(project)
    }

    pub async fn projects(
        &self,
        actor: &Principal<'_>,
    ) -> Result<Vec<Record>> {
        Ok(self.project_access(actor).await?.0)
    }

    /// Preserves drafts on owned edits without hiding policy-driven
    /// revocation.
    async fn project_access(
        &self,
        actor: &Principal<'_>,
    ) -> Result<(Vec<Record>, Vec<String>)> {
        self.verify(actor).await?;
        let roles = self
            .iam
            .effective_role_names_for_user(actor.tenant, actor.user)
            .await?;
        let mut result = Vec::new();
        let mut denied_owned = Vec::new();
        let mut after = None;
        for _ in 0..10 {
            let page = self
                .store
                .list(actor.tenant, Collection::Project, after.as_deref(), 100)
                .await?;
            let finished = page.len() < 100;
            after = page.last().map(|record| record.id.clone());
            for project in page {
                if project_visible(&project, actor.user, &roles) {
                    let allowed = self
                        .auth
                        .is_authorized(
                            actor.user,
                            actor.tenant,
                            Permission::View,
                            "project",
                            &project.id,
                            Some(actor.context),
                        )
                        .await?;
                    if allowed {
                        result.push(project);
                    } else if project.owner_id == actor.user {
                        denied_owned.push(project.id);
                    }
                }
            }
            if finished {
                break;
            }
        }
        Ok((result, denied_owned))
    }

    pub async fn save_project(
        &self,
        actor: &Principal<'_>,
        input: Value,
    ) -> Result<Value> {
        self.verify(actor).await?;
        let input = ProjectInput::parse(input)?;
        for role in &input.roles {
            ensure!(
                self.iam.role_exists(actor.tenant, role).await?,
                "Project role unavailable"
            );
        }
        let existing = match &input.id {
            Some(id) => Some(self.project(actor, id).await?),
            None => None,
        };
        if let Some(project) = &existing {
            ensure!(
                project.owner_id == actor.user,
                "Only the project owner can edit its settings"
            );
            ensure!(
                project.data.get("shared").and_then(Value::as_bool) ==
                    Some(input.shared),
                "Project visibility cannot be widened after creation"
            );
        }
        let record = Record {
            id: input
                .id
                .clone()
                .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string()),
            owner_id: actor.user.into(),
            project_id: None,
            expires_at: None,
            revision: 1,
            updated_at: now(),
            data: serde_json::to_value(&input)?,
        };
        let saved = self
            .store
            .save_project(
                actor.tenant,
                &record,
                input.revision,
                self.access.as_ref(),
            )
            .await?;
        tracing::info!(event.name = "pengolo.project.saved");
        Ok(serde_json::to_value(saved)?)
    }

    async fn authorize_record(
        &self,
        actor: &Principal<'_>,
        record: &Record,
    ) -> Result<()> {
        self.verify(actor).await?;
        if let Some(project) = &record.project_id {
            self.project(actor, project).await?;
        } else {
            ensure!(
                record.owner_id == actor.user,
                "Workspace resource unavailable"
            );
        }
        Ok(())
    }

    async fn file(
        &self,
        actor: &Principal<'_>,
        id: &str,
        temporary_session: Option<&str>,
        ready: bool,
    ) -> Result<Record> {
        let file = self
            .store
            .get(
                actor.tenant,
                Collection::File,
                id,
                temporary_session.is_some(),
            )
            .await?
            .context("File unavailable")?;
        self.authorize_record(actor, &file).await?;
        ensure!(
            file.data.get("temporarySession").and_then(Value::as_str) ==
                temporary_session,
            "File unavailable"
        );
        if ready {
            ensure!(
                file_available(
                    string(&file.data, "status")?,
                    file.expires_at.context("Missing file expiration")?,
                    now()
                ),
                "File unavailable"
            );
        }
        Ok(file)
    }

    pub async fn library(
        &self,
        actor: &Principal<'_>,
        project_id: Option<&str>,
        search: &str,
        after: Option<&str>,
    ) -> Result<Value> {
        self.verify(actor).await?;
        if let Some(id) = project_id {
            self.project(actor, id).await?;
        }
        ensure!(search.len() <= 128, "Search exceeds limit");
        let page = self
            .store
            .list_scoped(RecordQuery {
                tenant: actor.tenant,
                collection: Collection::File,
                project: project_id,
                owner: actor.user,
                search,
                after,
                limit: 50,
            })
            .await?;
        let cursor = if page.len() == 50 {
            page.last().map(|record| record.id.clone())
        } else {
            None
        };
        let files: Vec<_> = page
            .into_iter()
            .filter(|file| {
                file.project_id.as_deref() == project_id &&
                    (project_id.is_some() || file.owner_id == actor.user)
            })
            .collect();
        Ok(
            json!({"items":files.into_iter().map(file_projection).collect::<Vec<_>>(),"after":cursor}),
        )
    }

    pub async fn begin_upload(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        self.verify(actor).await?;
        let input: UploadInput = parse(value)?;
        let extension = validate_upload(&input.name, &input.mime, input.size)?;
        ensure!(
            base64::engine::general_purpose::STANDARD
                .decode(&input.checksum)
                .is_ok_and(|value| value.len() == 32),
            "Invalid upload checksum"
        );
        ensure!(
            input.temporary_session.is_none() || input.project_id.is_none(),
            "Temporary files cannot enter project libraries"
        );
        if let Some(project) = &input.project_id {
            self.project(actor, project).await?;
        }
        if let Some(session) = &input.temporary_session {
            ensure!(
                session.len() == 32 &&
                    session.bytes().all(|byte| byte.is_ascii_hexdigit()),
                "Invalid temporary session"
            );
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let key = if let Some(session) = &input.temporary_session {
            format!(
                "{}/temporary/{}/{session}/{id}.{extension}",
                actor.tenant,
                urlencoding::encode(actor.user)
            )
        } else if let Some(project) = &input.project_id {
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
        let temporary = input.temporary_session.is_some();
        let record = Record {
            id,
            owner_id: actor.user.into(),
            project_id: input.project_id,
            expires_at: Some(now() + STAGING_TTL_MS),
            revision: 1,
            updated_at: now(),
            data: json!({"name":input.name,"originalName":input.name,"mime":input.mime,"size":input.size,"key":key,"stagingKey":format!("{}/{}/pengolo/{}",actor.tenant,urlencoding::encode(actor.user),uuid::Uuid::new_v4().simple()),"stagingExpiresAt":now()+STAGING_TTL_MS,"checksum":input.checksum,"status":"uploading","temporarySession":input.temporary_session}),
        };
        let file = self
            .store
            .reserve_file(actor.tenant, &record, temporary)
            .await?;
        let (url, headers) = self
            .objects
            .upload_url(&staged_file(actor.tenant, &file)?)
            .await?;
        tracing::info!(event.name = "pengolo.file.upload_reserved");
        Ok(json!({"url":url,"headers":headers,"file":file_projection(file)}))
    }

    pub async fn finish_upload(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        let input: FileInput = parse(value)?;
        let file = self
            .file(actor, &input.id, input.temporary_session.as_deref(), false)
            .await?;
        ensure!(
            file.owner_id == actor.user,
            "Only the uploader can finalize a file"
        );
        if string(&file.data, "status")? == "ready" {
            return Ok(serde_json::to_value(file_projection(file))?);
        }
        let file = self
            .store
            .promote_file(
                actor.tenant,
                &file,
                input.temporary_session.is_some(),
                &staged_file(actor.tenant, &file)?,
                self.objects.as_ref(),
            )
            .await?;
        self.authorize_record(actor, &file).await?;
        tracing::info!(event.name = "pengolo.file.ready");
        Ok(serde_json::to_value(file_projection(file))?)
    }

    pub async fn download(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        let input: FileInput = parse(value)?;
        let file = self
            .file(actor, &input.id, input.temporary_session.as_deref(), true)
            .await?;
        Ok(
            json!({"url":file_url(&file.id,input.temporary_session.as_deref())}),
        )
    }

    pub async fn file_content(
        &self,
        actor: &Principal<'_>,
        id: &str,
        temporary_session: Option<&str>,
    ) -> Result<(Record, crate::application::ports::pengolo_store::FileChunks)>
    {
        let file = self.file(actor, id, temporary_session, true).await?;
        let chunks = self.objects.read(string(&file.data, "key")?).await?;
        Ok((file, chunks))
    }

    pub async fn delete_file(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        let input: FileInput = parse(value)?;
        let mut file = self
            .file(actor, &input.id, input.temporary_session.as_deref(), false)
            .await?;
        if file.owner_id != actor.user {
            let project = self
                .project(
                    actor,
                    file.project_id.as_deref().context("File unavailable")?,
                )
                .await?;
            ensure!(
                project.owner_id == actor.user,
                "File deletion unavailable"
            );
        }
        let revision = file.revision;
        file.expires_at = Some(now());
        file.data
            .as_object_mut()
            .context("Invalid file metadata")?
            .insert("status".into(), json!("deleted"));
        self.store
            .save(
                actor.tenant,
                Collection::File,
                &file,
                input.temporary_session.is_some(),
                Some(revision),
            )
            .await?;
        self.store
            .purge_file(
                actor.tenant,
                &input.id,
                input.temporary_session.is_some(),
                false,
                self.objects.as_ref(),
            )
            .await?;
        tracing::info!(event.name = "pengolo.file.deleted");
        Ok(json!(true))
    }

    async fn evidence_visible(
        &self,
        actor: &Principal<'_>,
        sources: &[EvidenceSource],
    ) -> Result<bool> {
        for source in sources {
            if matches!(
                source.resource_type.as_str(),
                "pengolo_file" | "pengolo_temporary_file"
            ) {
                if self
                    .file(
                        actor,
                        &source.resource_id,
                        if source.resource_type == "pengolo_temporary_file" {
                            source.entity_id.as_deref()
                        } else {
                            None
                        },
                        true,
                    )
                    .await
                    .is_err()
                {
                    return Ok(false);
                }
            } else {
                let context = QueryContext {
                    entity_id: source.entity_id.clone(),
                    modality: source.modality.clone(),
                    state_type: source.state_type.clone(),
                    ..actor.context.clone()
                };
                if !self
                    .auth
                    .is_authorized(
                        actor.user,
                        actor.tenant,
                        if source.manage {
                            Permission::Manage
                        } else {
                            Permission::View
                        },
                        &source.resource_type,
                        &source.resource_id,
                        Some(&context),
                    )
                    .await?
                {
                    return Ok(false);
                }
            }
        }
        Ok(true)
    }

    pub async fn chat(
        &self,
        actor: &Principal<'_>,
        id: &str,
        temporary: bool,
    ) -> Result<Record> {
        let mut chat = self
            .store
            .get(actor.tenant, Collection::Chat, id, temporary)
            .await?
            .context("Conversation unavailable")?;
        self.authorize_record(actor, &chat).await?;
        let mut visible = Vec::new();
        if let Some(messages) =
            chat.data.get("messages").and_then(Value::as_array)
        {
            for message in messages {
                let evidence: Vec<EvidenceSource> = message
                    .get("evidence")
                    .cloned()
                    .map(serde_json::from_value)
                    .transpose()?
                    .unwrap_or_default();
                if self.evidence_visible(actor, &evidence).await? {
                    visible.push(message.clone());
                }
            }
        }
        chat.data
            .as_object_mut()
            .context("Invalid conversation")?
            .insert("messages".into(), json!(visible));
        Ok(chat)
    }

    pub async fn chats(
        &self,
        actor: &Principal<'_>,
        after: Option<&str>,
        project_id: Option<&str>,
    ) -> Result<Value> {
        self.verify(actor).await?;
        self.store.expire_leases(actor.tenant).await?;
        if let Some(project) = project_id {
            self.project(actor, project).await?;
        }
        let page = self
            .store
            .list_scoped(RecordQuery {
                tenant: actor.tenant,
                collection: Collection::Chat,
                project: project_id,
                owner: actor.user,
                search: "",
                after,
                limit: 50,
            })
            .await?;
        let cursor = if page.len() == 50 {
            page.last().map(|record| record.id.clone())
        } else {
            None
        };
        let mut items = Vec::new();
        for mut record in page {
            if self.authorize_record(actor, &record).await.is_ok() {
                record
                    .data
                    .as_object_mut()
                    .context("Invalid conversation")?
                    .remove("messages");
                items.push(record);
            }
        }
        Ok(json!({"items":items,"after":cursor}))
    }
}

impl PengoloService {
    async fn document_project(
        &self,
        actor: &Principal<'_>,
        input: &SendInput,
    ) -> Result<Option<String>> {
        if let Some(id) = &input.project_id {
            let project = self.project(actor, id).await?;
            if project.data.get("contextMode").and_then(Value::as_str) ==
                Some("personal")
            {
                ensure!(
                    project.owner_id == actor.user &&
                        project.data.get("shared").and_then(Value::as_bool) ==
                            Some(false),
                    "Personal context unavailable"
                );
                return Ok(None);
            }
            return Ok(Some(id.clone()));
        }
        Ok(None)
    }

    async fn attachment(
        &self,
        actor: &Principal<'_>,
        input: &SendInput,
        id: &str,
    ) -> Result<(Value, EvidenceSource)> {
        let temporary_session = input
            .temporary
            .then_some(input.chat_id.as_deref())
            .flatten();
        let file = match self.file(actor, id, temporary_session, true).await {
            Ok(file) => file,
            Err(error) => {
                if !input.temporary || !input.with_library {
                    return Err(error);
                }
                self.file(actor, id, None, true).await?
            },
        };
        let selected = self.document_project(actor, input).await?;
        let temporary_file =
            file.data.get("temporarySession").and_then(Value::as_str);
        ensure!(
            document_scope_allows(
                selected.as_deref(),
                actor.user,
                temporary_session
                    .or(input.temporary.then_some("new-temporary-session")),
                input.with_library,
                file.project_id.as_deref(),
                &file.owner_id,
                temporary_file
            ),
            "Document is outside this conversation's library"
        );
        let remaining = file
            .expires_at
            .context("Missing expiration")?
            .saturating_sub(now()) /
            1000;
        ensure!(remaining > 0, "File expired");
        let mime = string(&file.data, "mime")?;
        let attachment = if matches!(mime, "text/plain" | "text/csv") {
            ensure!(
                file.data
                    .get("size")
                    .and_then(Value::as_i64)
                    .is_some_and(|size| size <= 65536),
                "Text document exceeds context limit"
            );
            let mut chunks =
                self.objects.read(string(&file.data, "key")?).await?;
            let mut body = Vec::with_capacity(8192);
            while let Some(chunk) = chunks.next().await {
                let chunk = chunk?;
                ensure!(
                    body.len() + chunk.len() <= 65536,
                    "Text document exceeds context limit"
                );
                body.extend_from_slice(&chunk);
            }
            let text = String::from_utf8(body)
                .context("Text document must use UTF-8")?;
            json!({"kind":"text","text":text})
        } else {
            let kind = if mime.starts_with("image/") {
                "image"
            } else {
                "document"
            };
            let url = self
                .objects
                .download_url(
                    string(&file.data, "key")?,
                    u64::try_from(remaining.min(300))?,
                )
                .await?;
            json!({"kind":kind,"url":url,"media_type":mime,"trusted_storage":true})
        };
        Ok((
            attachment,
            EvidenceSource {
                resource_type: if temporary_file.is_some() {
                    "pengolo_temporary_file"
                } else {
                    "pengolo_file"
                }
                .into(),
                resource_id: id.into(),
                manage: false,
                entity_id: temporary_file.map(str::to_owned),
                modality: None,
                state_type: None,
            },
        ))
    }

    pub async fn send(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        self.verify(actor).await?;
        let input = SendInput::parse(value)?;
        let id = uuid::Uuid::parse_str(&input.idempotency_key)?
            .simple()
            .to_string();
        if let Some(generation) = self
            .store
            .get(actor.tenant, Collection::Generation, &id, input.temporary)
            .await?
        {
            ensure!(
                generation.owner_id == actor.user &&
                    generation.data.get("input") ==
                        Some(&serde_json::to_value(&input)?),
                "Idempotency conflict"
            );
            self.authorize_generation(actor, &generation, input.temporary)
                .await?;
            return Ok(
                json!({"chat":self.chat(actor,string(&generation.data,"chatId")?,input.temporary).await?,"generation":generation_projection(&generation)}),
            );
        }
        let catalog = self.models(actor).await?;
        ensure!(
            catalog
                .models
                .iter()
                .any(|model| model.alias == input.model_alias),
            "Select an available model with medium reasoning"
        );
        if let Some(id) = &input.project_id {
            self.project(actor, id).await?;
        }
        self.store.expire_leases(actor.tenant).await?;
        let existing = if let Some(id) = &input.chat_id {
            self.store
                .get(actor.tenant, Collection::Chat, id, input.temporary)
                .await?
        } else {
            None
        };
        if let Some(chat) = &existing {
            self.authorize_record(actor, chat).await?;
            ensure!(
                chat.project_id == input.project_id,
                "Conversation project changed"
            );
        }
        let mut attachments = Vec::with_capacity(input.file_ids.len());
        let mut sources = Vec::new();
        for id in &input.file_ids {
            let (attachment, source) =
                self.attachment(actor, &input, id).await?;
            attachments.push(attachment);
            sources.push(source);
        }
        let mut history = Vec::new();
        let mut bytes = 0;
        if let Some(chat) = existing {
            let chat = self.chat(actor, &chat.id, input.temporary).await?;
            if let Some(messages) =
                chat.data.get("messages").and_then(Value::as_array)
            {
                for message in messages.iter().rev() {
                    let text = string(message, "text")?;
                    if bytes + text.len() > 32768 || history.len() >= 64 {
                        break;
                    }
                    if message
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| status != "completed")
                    {
                        continue;
                    }
                    let evidence: Vec<EvidenceSource> = message
                        .get("evidence")
                        .cloned()
                        .map(serde_json::from_value)
                        .transpose()?
                        .unwrap_or_default();
                    if !self.evidence_visible(actor, &evidence).await? {
                        continue;
                    }
                    let mut files = Vec::new();
                    let mut visible = true;
                    if let Some(ids) =
                        message.get("fileIds").and_then(Value::as_array)
                    {
                        for id in ids.iter().filter_map(Value::as_str) {
                            match self.attachment(actor, &input, id).await {
                                Ok((file, source)) => {
                                    files.push(file);
                                    sources.push(source);
                                },
                                Err(_) => {
                                    visible = false;
                                    break;
                                },
                            }
                        }
                    }
                    if !visible {
                        continue;
                    }
                    sources.extend(evidence);
                    bytes += text.len();
                    history.push(json!({"role":message.get("role"),"content":text,"attachments":files}));
                }
            }
        }
        history.reverse();
        let capability = format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        let reservation = self
            .store
            .reserve(actor.tenant, actor.user, &input, &capability)
            .await?;
        if reservation.created {
            self.store
                .record_evidence(
                    actor.tenant,
                    &reservation.generation.id,
                    input.temporary,
                    &sources,
                )
                .await?;
            let mut generation = self
                .store
                .get(
                    actor.tenant,
                    Collection::Generation,
                    &reservation.generation.id,
                    input.temporary,
                )
                .await?
                .context("Generation unavailable")?;
            let revision = generation.revision;
            generation
                .data
                .as_object_mut()
                .context("Invalid generation")?
                .insert(
                    "authorization".into(),
                    serde_json::to_value(actor.context)?,
                );
            self.store
                .save(
                    actor.tenant,
                    Collection::Generation,
                    &generation,
                    input.temporary,
                    Some(revision),
                )
                .await?;
            let request = json!({"id":reservation.generation.id,"prompt":input.prompt,"model_alias":input.model_alias,"reasoning_effort":"medium","capability":capability,"history":history,"attachments":attachments,"graph_context":input.graph_context,"web_query":input.web_query,"library_enabled":true});
            if self.agent.submit_generation(request).await.is_err() {
                self.store
                    .append(
                        actor.tenant,
                        &reservation.generation.id,
                        input.temporary,
                        &OutputEvent {
                            sequence: -1,
                            kind: "failed".into(),
                            content: String::new(),
                        },
                    )
                    .await?;
                tracing::warn!(
                    event.name = "pengolo.generation.submission_failed"
                );
            } else {
                tracing::info!(event.name = "pengolo.generation.submitted");
            }
        }
        let generation = self
            .store
            .get(
                actor.tenant,
                Collection::Generation,
                &reservation.generation.id,
                input.temporary,
            )
            .await?
            .context("Generation unavailable")?;
        Ok(
            json!({"chat":self.chat(actor,&reservation.chat.id,input.temporary).await?,"generation":generation_projection(&generation)}),
        )
    }

    pub async fn authorize_generation(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        temporary: bool,
    ) -> Result<Record> {
        self.authorize_record(actor, generation).await?;
        let chat = self
            .store
            .get(
                actor.tenant,
                Collection::Chat,
                string(&generation.data, "chatId")?,
                temporary,
            )
            .await?
            .context("Conversation unavailable")?;
        self.authorize_record(actor, &chat).await?;
        ensure!(
            chat.project_id == generation.project_id,
            "Generation scope changed"
        );
        let sources: Vec<EvidenceSource> = parse(
            generation
                .data
                .get("evidence")
                .cloned()
                .context("Missing generation provenance")?,
        )?;
        ensure!(
            self.evidence_visible(actor, &sources).await?,
            "Generation evidence is no longer available"
        );
        Ok(chat)
    }

    pub async fn events(
        &self,
        actor: &Principal<'_>,
        id: &str,
        after: i32,
        temporary: bool,
    ) -> Result<Value> {
        ensure!(after >= 0, "Invalid event cursor");
        self.verify(actor).await?;
        self.store.expire_leases(actor.tenant).await?;
        let generation = self
            .store
            .get(actor.tenant, Collection::Generation, id, temporary)
            .await?
            .context("Generation unavailable")?;
        let mut chat = self
            .authorize_generation(actor, &generation, temporary)
            .await?;
        let events = self
            .store
            .events(actor.tenant, id, temporary, after)
            .await?;
        if events.last().is_some_and(|event| {
            matches!(event.kind.as_str(), "completed" | "failed" | "cancelled")
        }) {
            // Completion and its assistant message commit atomically after the
            // earlier authorization snapshot.
            chat = self.chat(actor, &chat.id, temporary).await?;
        }
        let complete = events
            .last()
            .map_or(i64::from(after), |event| i64::from(event.sequence)) >=
            generation
                .data
                .get("sequence")
                .and_then(Value::as_i64)
                .unwrap_or(0);
        let status = if complete {
            generation
                .data
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("failed")
        } else {
            "running"
        };
        Ok(replay_projection(&chat, id, status, &events))
    }

    pub async fn accept_output(
        &self,
        capability: &str,
        generation_id: &str,
        event: OutputEvent,
    ) -> Result<()> {
        let (tenant, generation, temporary) =
            self.store.resolve_capability(capability).await?;
        ensure!(
            generation.id == generation_id,
            "Invalid generation capability"
        );
        let mut context: QueryContext = generation
            .data
            .get("authorization")
            .cloned()
            .map(serde_json::from_value)
            .transpose()?
            .unwrap_or_default();
        context.hour_utc = i64::from(chrono::Utc::now().hour());
        let actor = Principal {
            tenant: &tenant,
            user: &generation.owner_id,
            context: &context,
        };
        self.authorize_generation(&actor, &generation, temporary)
            .await?;
        if event.kind == "tool" {
            ensure!(
                matches!(
                    event.content.as_str(),
                    "context" |
                        "web" |
                        "search" |
                        "graph" |
                        "causal" |
                        "library" |
                        "run_python" |
                        "mcp"
                ),
                "Invalid tool progress"
            );
        }
        self.store
            .append(&tenant, generation_id, temporary, &event)
            .await?;
        if event.sequence != 0 {
            self.notify_output(generation_id);
        }
        Ok(())
    }

    pub async fn cancel(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        let input: CancelInput = parse(value)?;
        let generation = self
            .store
            .get(
                actor.tenant,
                Collection::Generation,
                &input.id,
                input.temporary,
            )
            .await?
            .context("Generation unavailable")?;
        self.authorize_generation(actor, &generation, input.temporary)
            .await?;
        if matches!(
            generation.data.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        ) {
            let capability = self
                .store
                .generation_capability(actor.tenant, &input.id)
                .await?;
            let sequence = -1;
            self.store
                .append(
                    actor.tenant,
                    &input.id,
                    input.temporary,
                    &OutputEvent {
                        sequence,
                        kind: "cancelled".into(),
                        content: String::new(),
                    },
                )
                .await?;
            if let Some(capability) = capability &&
                self.agent
                    .cancel_generation(&input.id, &capability)
                    .await
                    .is_err()
            {
                tracing::warn!(
                    event.name = "pengolo.cancel.awaiting_heartbeat"
                );
            }
            tracing::info!(event.name = "pengolo.generation.cancelled");
            self.notify_output(&input.id);
        }
        Ok(json!(true))
    }

    pub async fn update_chat(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        let input: ChatInput = parse(value)?;
        let mut chat = self
            .store
            .get(actor.tenant, Collection::Chat, &input.id, input.temporary)
            .await?
            .context("Conversation unavailable")?;
        self.authorize_record(actor, &chat).await?;
        ensure!(
            chat.owner_id == actor.user,
            "Only the conversation owner can change it"
        );
        ensure!(
            chat.revision == input.revision,
            "Conversation revision conflict"
        );
        if let Some(checks) = input.task_checks {
            save_task_checks(
                &mut chat.data,
                input.message_id.as_deref().context("Message is required")?,
                &checks,
            )?;
        }
        if let Some(title) = input.title {
            ensure!(
                !title.trim().is_empty() && title.len() <= 256,
                "Invalid conversation title"
            );
            chat.data
                .as_object_mut()
                .context("Invalid conversation")?
                .insert("title".into(), json!(title.trim()));
        }
        if let Some(pinned) = input.pinned {
            chat.data
                .as_object_mut()
                .context("Invalid conversation")?
                .insert("pinned".into(), json!(pinned));
        }
        if input.move_project {
            ensure!(!input.temporary, "Temporary chats cannot be moved");
            ensure!(
                chat.data.get("activeGeneration").is_none_or(Value::is_null),
                "Wait for the active generation before moving this chat"
            );
            let document_project = if let Some(project) = &input.project_id {
                let project = self.project(actor, project).await?;
                if project.data.get("contextMode").and_then(Value::as_str) ==
                    Some("personal")
                {
                    None
                } else {
                    input.project_id.as_deref()
                }
            } else {
                None
            };
            if let Some(messages) =
                chat.data.get("messages").and_then(Value::as_array)
            {
                for message in messages {
                    let mut ids: Vec<&str> = message
                        .get("fileIds")
                        .and_then(Value::as_array)
                        .map(|ids| {
                            ids.iter().filter_map(Value::as_str).collect()
                        })
                        .unwrap_or_default();
                    if let Some(evidence) =
                        message.get("evidence").and_then(Value::as_array)
                    {
                        ids.extend(
                            evidence
                                .iter()
                                .filter(|source| {
                                    source
                                        .get("resource_type")
                                        .and_then(Value::as_str) ==
                                        Some("pengolo_file")
                                })
                                .filter_map(|source| {
                                    source
                                        .get("resource_id")
                                        .and_then(Value::as_str)
                                }),
                        );
                    }
                    for id in ids {
                        let file = self.file(actor, id, None, true).await?;
                        ensure!(
                            document_scope_allows(
                                document_project,
                                actor.user,
                                None,
                                false,
                                file.project_id.as_deref(),
                                &file.owner_id,
                                None
                            ),
                            "Move would disclose a document outside its library"
                        );
                    }
                }
            }
            chat.project_id = input.project_id;
        }
        let saved = self
            .store
            .save(
                actor.tenant,
                Collection::Chat,
                &chat,
                input.temporary,
                Some(input.revision),
            )
            .await?;
        tracing::info!(event.name = "pengolo.chat.updated");
        Ok(serde_json::to_value(saved)?)
    }

    pub async fn close_temporary(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        self.verify(actor).await?;
        let input: FileInput = parse(value)?;
        self.store
            .expire_temporary(actor.tenant, actor.user, &input.id)
            .await?;
        tracing::info!(event.name = "pengolo.temporary.closed");
        Ok(json!(true))
    }

    pub async fn delete_chat(
        &self,
        actor: &Principal<'_>,
        value: Value,
    ) -> Result<Value> {
        let input: ChatInput = parse(value)?;
        let chat = self.chat(actor, &input.id, false).await?;
        ensure!(
            chat.owner_id == actor.user && chat.revision == input.revision,
            "Conversation deletion unavailable"
        );
        ensure!(
            chat.data.get("activeGeneration").is_none_or(Value::is_null),
            "Stop the active generation before deleting this chat"
        );
        self.store
            .remove(actor.tenant, Collection::Chat, &input.id, false)
            .await?;
        tracing::info!(event.name = "pengolo.chat.deleted");
        Ok(json!(true))
    }

    pub async fn cleanup(&self) -> Result<()> {
        for tenant in self.store.cleanup_tenants().await? {
            self.store.expire_leases(&tenant).await?;
            for expiry in self.store.expired_objects(&tenant).await? {
                if self
                    .store
                    .purge_file(
                        &tenant,
                        &expiry.id,
                        expiry.temporary,
                        expiry.staging,
                        self.objects.as_ref(),
                    )
                    .await
                    .is_err()
                {
                    tracing::warn!(event.name = "pengolo.file.purge_deferred");
                }
            }
            for (collection, record, temporary) in
                self.store.expired(&tenant).await?
            {
                if matches!(collection, Collection::File) {
                    continue;
                }
                self.store
                    .remove(&tenant, collection, &record.id, temporary)
                    .await?;
            }
        }
        Ok(())
    }

    pub async fn library_tool(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        query: &str,
        after: Option<&str>,
    ) -> Result<Value> {
        ensure!(query.len() <= 128, "Library search exceeds limit");
        let input: SendInput = parse(
            generation
                .data
                .get("input")
                .cloned()
                .context("Missing generation input")?,
        )?;
        let project = self.document_project(actor, &input).await?;
        if input.temporary && !input.with_library {
            return Ok(json!({"files":[],"after":null}));
        }
        let files = self
            .store
            .list_scoped(RecordQuery {
                tenant: actor.tenant,
                collection: Collection::File,
                project: project.as_deref(),
                owner: actor.user,
                search: query,
                after,
                limit: 8,
            })
            .await?;
        let cursor = if files.len() == 8 {
            files.last().map(|file| file.id.clone())
        } else {
            None
        };
        let mut output = Vec::new();
        let mut evidence = Vec::new();
        for file in files
            .iter()
            .filter(|file| {
                file.data.get("status").and_then(Value::as_str) ==
                    Some("ready")
            })
            .take(8)
        {
            let (attachment, source) =
                self.attachment(actor, &input, &file.id).await?;
            evidence.push(source);
            output.push(json!({"id":file.id,"name":file.data.get("name"),"attachment":attachment}));
        }
        self.store
            .record_evidence(
                actor.tenant,
                &generation.id,
                input.temporary,
                &evidence,
            )
            .await?;
        Ok(json!({"files":output,"after":cursor}))
    }
}

impl PengoloService {
    pub async fn web_search(&self, query: &str) -> Result<Value> {
        ensure!(
            !query.trim().is_empty() && query.len() <= 512,
            "Invalid public web query"
        );
        let endpoint = std::env::var("PENGOLO_WEB_SEARCH_URL")
            .context("Web search is not configured")?;
        let url = reqwest::Url::parse(&endpoint)
            .context("Invalid web search endpoint")?;
        ensure!(
            url.scheme() == "https" &&
                url.username().is_empty() &&
                url.password().is_none() &&
                url.query().is_none() &&
                url.fragment().is_none(),
            "Invalid web search endpoint"
        );
        let mut response = outbound_client()?
            .post(url)
            .json(&json!({"query":query}))
            .send()
            .await
            .context("Web search unavailable")?;
        ensure!(response.status().is_success(), "Web search unavailable");
        let mut body = Vec::with_capacity(8192);
        while let Some(chunk) = response.chunk().await? {
            ensure!(
                body.len() + chunk.len() <= 262144,
                "Web search result exceeds limit"
            );
            body.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&body)
            .map_err(|_| anyhow::anyhow!("Invalid web search result"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_checks_only_update_terminal_assistant_messages() -> Result<()> {
        let mut data = json!({"messages":[{"id":"user","role":"user","text":"- [ ] User"},{"id":"answer","role":"assistant","text":"- [ ] Task","status":"completed"}]});
        let checks: Vec<TaskCheck> =
            serde_json::from_value(json!([{"index":0,"checked":true}]))?;
        assert!(save_task_checks(&mut data, "user", &checks).is_err());
        save_task_checks(&mut data, "answer", &checks)?;
        assert_eq!(
            data.pointer("/messages/1/taskChecks/0/checked"),
            Some(&json!(true))
        );
        assert_eq!(
            data.pointer("/messages/1/text"),
            Some(&json!("- [ ] Task"))
        );
        let invalid: Vec<TaskCheck> = serde_json::from_value(
            json!([{"index":0,"checked":true},{"index":0,"checked":false}]),
        )?;
        assert!(save_task_checks(&mut data, "answer", &invalid).is_err());
        Ok(())
    }

    #[test]
    fn workspace_version_is_opaque_and_independent_of_project_order() {
        let roles = vec!["analyst".to_owned()];
        let first = workspace_version(
            "tenant",
            "user",
            &roles,
            &[
                version_project("b", "other", 2),
                version_project("a", "other", 1),
            ],
            Vec::new(),
        );
        assert_eq!(
            first,
            workspace_version(
                "tenant",
                "user",
                &roles,
                &[
                    version_project("a", "other", 1),
                    version_project("b", "other", 2)
                ],
                Vec::new()
            )
        );
        assert_ne!(
            first,
            workspace_version(
                "tenant",
                "user",
                &[],
                &[
                    version_project("a", "other", 1),
                    version_project("b", "other", 2)
                ],
                Vec::new()
            )
        );
        assert!(!first.contains("analyst"));
        assert!(!first.contains("tenant"));
    }

    fn version_project(id: &str, owner: &str, revision: i64) -> Record {
        Record {
            id: id.into(),
            owner_id: owner.into(),
            revision,
            updated_at: 0,
            project_id: None,
            expires_at: None,
            data: json!({}),
        }
    }

    #[test]
    fn owned_project_edits_preserve_drafts_but_shared_access_invalidates_caches()
     {
        let roles = vec!["staff".into()];
        let original =
            workspace_version("tenant", "owner", &roles, &[], Vec::new());
        assert_eq!(
            original,
            workspace_version(
                "tenant",
                "owner",
                &roles,
                &[version_project("own", "owner", 1)],
                Vec::new()
            )
        );
        assert_eq!(
            original,
            workspace_version(
                "tenant",
                "owner",
                &roles,
                &[version_project("own", "owner", 2)],
                Vec::new()
            )
        );
        let shared = workspace_version(
            "tenant",
            "owner",
            &roles,
            &[version_project("shared", "other", 1)],
            Vec::new(),
        );
        assert_ne!(original, shared);
        assert_ne!(
            shared,
            workspace_version(
                "tenant",
                "owner",
                &roles,
                &[version_project("shared", "other", 2)],
                Vec::new()
            )
        );
    }

    #[test]
    fn owned_project_revocation_invalidates_caches_without_role_changes() {
        let allowed =
            workspace_version("tenant", "owner", &[], &[], Vec::new());
        let denied =
            workspace_version("tenant", "owner", &[], &[], vec!["own".into()]);
        assert_ne!(allowed, denied);
    }

    #[test]
    fn terminal_replay_only_contains_the_matching_assistant_message()
    -> Result<()> {
        let chat = Record {
            id: "chat".into(),
            owner_id: "user".into(),
            project_id: None,
            expires_at: None,
            revision: 4,
            updated_at: 42,
            data: json!({"messages":[{"id":"old","role":"assistant","text":"private old history","generationId":"old-run"},{"id":"user","role":"user","text":"question","generationId":"run"},{"id":"answer","role":"assistant","text":"answer","generationId":"run","status":"completed"}]}),
        };
        let running = replay_projection(&chat, "run", "running", &[]);
        assert!(running.get("message").is_none_or(Value::is_null));
        let finished = replay_projection(&chat, "run", "completed", &[]);
        assert_eq!(finished.pointer("/message/id"), Some(&json!("answer")));
        assert_eq!(finished.get("chatRevision"), Some(&json!(4)));
        assert!(
            !serde_json::to_string(&finished)?.contains("private old history")
        );
        let overlapping = replay_projection(
            &chat,
            "run",
            "running",
            &[OutputEvent {
                sequence: 1,
                kind: "completed".into(),
                content: String::new(),
            }],
        );
        assert_eq!(overlapping.get("status"), Some(&json!("completed")));
        assert_eq!(overlapping.pointer("/message/id"), Some(&json!("answer")));
        Ok(())
    }

    #[tokio::test]
    async fn durable_output_notifications_wake_receivers_without_polling()
    -> Result<()> {
        let signals = WorkspaceSignals::new();
        let mut output = signals.output.subscribe();
        signals
            .output
            .send(Arc::from("run"))
            .map_err(|_| anyhow::anyhow!("Receiver missing"))?;
        assert_eq!(output.recv().await?.as_ref(), "run");
        let mut identity = signals.identity.subscribe();
        signals
            .identity
            .send_modify(|version| *version = version.wrapping_add(1));
        identity.changed().await?;
        Ok(())
    }

    #[tokio::test]
    async fn unrelated_generation_output_cannot_trigger_a_database_replay()
    -> Result<()> {
        let signals = WorkspaceSignals::new();
        let mut changes = signals.output.subscribe();
        signals
            .output
            .send(Arc::from("other"))
            .map_err(|_| anyhow::anyhow!("Receiver missing"))?;
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(5),
                wait_for_output(&mut changes, "run")
            )
            .await
            .is_err()
        );
        signals
            .output
            .send(Arc::from("run"))
            .map_err(|_| anyhow::anyhow!("Receiver missing"))?;
        tokio::time::timeout(
            std::time::Duration::from_millis(50),
            wait_for_output(&mut changes, "run"),
        )
        .await?;
        Ok(())
    }

    #[test]
    fn project_visibility_uses_live_roles_and_exact_personal_ownership() {
        let personal = Record {
            id: "a".repeat(32),
            owner_id: "owner".into(),
            project_id: None,
            expires_at: None,
            revision: 1,
            updated_at: 0,
            data: serde_json::json!({"shared":false,"roles":[]}),
        };
        assert!(!project_visible(
            &personal,
            "administrator",
            &["administrator".into()]
        ));
        assert!(project_visible(&personal, "owner", &[]));
        let shared = Record {
            data: serde_json::json!({"shared":true,"roles":["analyst"]}),
            ..personal
        };
        assert!(project_visible(&shared, "reader", &["analyst".into()]));
        assert!(!project_visible(&shared, "reader", &[]));
    }

    #[test]
    fn generation_projection_cannot_expose_prompts_or_capabilities()
    -> anyhow::Result<()> {
        let record = Record {
            id: "a".repeat(32),
            owner_id: "owner".into(),
            project_id: None,
            expires_at: None,
            revision: 1,
            updated_at: 0,
            data: serde_json::json!({"input":{"prompt":"private","capability":"secret"},"status":"running","chatId":"c","sequence":1,"response":"answer","evidence":[]}),
        };
        let output = serde_json::to_string(&generation_projection(&record))?;
        assert!(!output.contains("private"));
        assert!(!output.contains("secret"));
        assert!(output.contains("running"));
        Ok(())
    }
}
