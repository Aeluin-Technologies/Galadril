//! Checkpoints and source archives remain private to an authorized generation.

use sha2::{Digest, Sha256};

use super::*;
use crate::domain::pengolo::{MAX_AGENT_STATE_BYTES, NORMAL_TTL_MS};

fn state_id(generation: &str, key: &str) -> String {
    format!(
        "{:x}",
        Sha256::digest(format!("agent-state/{generation}/{key}"))
    )
    .chars()
    .take(32)
    .collect()
}

impl PengoloService {
    /// Exposes only explicitly disclosed reports after source reauthorization.
    #[tracing::instrument(skip_all)]
    pub async fn specialist_report(
        &self,
        actor: &Principal<'_>,
        generation_id: &str,
        report_id: &str,
        temporary: bool,
    ) -> Result<Value> {
        self.verify(actor).await?;
        ensure!(
            report_id.len() == 64 &&
                report_id.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "Invalid report reference"
        );
        let generation = self
            .store
            .get(
                actor.tenant,
                Collection::Generation,
                generation_id,
                temporary,
            )
            .await?
            .context("Generation unavailable")?;
        let report = self
            .agent_state(
                actor,
                &generation,
                &format!("public_report:{report_id}"),
                None,
            )
            .await?;
        ensure!(
            report.get("id").and_then(Value::as_str) == Some(report_id) &&
                report
                    .get("agentId")
                    .and_then(Value::as_str)
                    .is_some_and(|id| !id.is_empty() && id.len() <= 64) &&
                report
                    .get("content")
                    .and_then(Value::as_str)
                    .is_some_and(|content| content.chars().count() <= 65536),
            "Specialist report unavailable"
        );
        tracing::info!(event.name = "pengolo.specialist_report.read");
        Ok(json!({
            "generationId": generation.id, "status": generation.data.get("status"),
            "events": [], "message": null, "chatRevision": null, "chatUpdatedAt": null,
            "specialistReport": report,
        }))
    }

    #[tracing::instrument(skip_all)]
    pub async fn agent_state(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        key: &str,
        value: Option<Value>,
    ) -> Result<Value> {
        ensure!(
            !key.is_empty() &&
                key.len() <= 256 &&
                !key.chars().any(char::is_control),
            "Invalid state key"
        );
        let input: SendInput = parse(
            generation
                .data
                .get("input")
                .cloned()
                .context("Missing generation input")?,
        )?;
        self.authorize_generation(actor, generation, input.temporary)
            .await?;
        ensure!(
            generation.owner_id == actor.user,
            "State requires generation ownership"
        );
        if let Some(value) = value {
            ensure!(
                matches!(
                    generation.data.get("status").and_then(Value::as_str),
                    Some("pending" | "running")
                ),
                "Generation is no longer active"
            );
            ensure!(
                serde_json::to_vec(&value)?.len() <= MAX_AGENT_STATE_BYTES,
                "State exceeds limit"
            );
            let id = state_id(&generation.id, key);
            let existing = self
                .store
                .get(
                    actor.tenant,
                    Collection::AgentState,
                    &id,
                    input.temporary,
                )
                .await?;
            if let Some(existing) = &existing {
                ensure!(existing.owner_id == actor.user, "State unavailable");
                if existing.data.get("value") == Some(&value) {
                    return Ok(json!(true));
                }
                ensure!(
                    key == "checkpoint" || key.starts_with("native:"),
                    "Evidence archives are immutable"
                );
            }
            let record = Record {
                id,
                owner_id: actor.user.into(),
                project_id: generation.project_id.clone(),
                expires_at: Some(
                    generation.expires_at.unwrap_or(now() + NORMAL_TTL_MS),
                ),
                revision: 1,
                updated_at: now(),
                data: json!({"generation":generation.id,"chatId":generation.data.get("chatId"),"temporarySession":if input.temporary {generation.data.get("chatId")} else {None},"key":key,"value":value}),
            };
            self.store
                .save(
                    actor.tenant,
                    Collection::AgentState,
                    &record,
                    input.temporary,
                    existing.map(|item| item.revision),
                )
                .await?;
            tracing::info!(
                event.name = "pengolo.agent_state.saved",
                checkpoint = key == "checkpoint"
            );
            return Ok(json!(true));
        }
        let mut current = generation.clone();
        for _ in 0..16 {
            self.authorize_generation(actor, &current, input.temporary)
                .await?;
            ensure!(
                current.owner_id == actor.user &&
                    current.project_id == generation.project_id &&
                    current.data.get("chatId") ==
                        generation.data.get("chatId"),
                "Recovery scope changed"
            );
            if let Some(record) = self
                .store
                .get(
                    actor.tenant,
                    Collection::AgentState,
                    &state_id(&current.id, key),
                    input.temporary,
                )
                .await?
            {
                ensure!(
                    record.owner_id == actor.user &&
                        record.data.get("generation") ==
                            Some(&json!(current.id)),
                    "State unavailable"
                );
                return record
                    .data
                    .get("value")
                    .cloned()
                    .context("Invalid state");
            }
            let Some(parent) = current
                .data
                .pointer("/input/resumeSource")
                .and_then(Value::as_str)
            else {
                return Ok(Value::Null);
            };
            current = self
                .store
                .get(
                    actor.tenant,
                    Collection::Generation,
                    parent,
                    input.temporary,
                )
                .await?
                .context("Recovery source unavailable")?;
        }
        anyhow::bail!("Recovery ancestry exceeds limit")
    }
}

impl PengoloService {
    pub(super) async fn resume_state(
        &self,
        actor: &Principal<'_>,
        input: &mut SendInput,
    ) -> Result<Option<String>> {
        let Some(source) = &input.resume_source else {
            return Ok(None);
        };
        let generation = self
            .store
            .get(
                actor.tenant,
                Collection::Generation,
                source,
                input.temporary,
            )
            .await?
            .context("Recovery source unavailable")?;
        ensure!(
            generation.owner_id == actor.user &&
                generation.project_id == input.project_id &&
                generation.data.get("chatId").and_then(Value::as_str) ==
                    input.chat_id.as_deref(),
            "Recovery scope changed"
        );
        ensure!(
            matches!(
                generation.data.get("status").and_then(Value::as_str),
                Some("failed" | "cancelled")
            ),
            "Only interrupted investigations can be recovered"
        );
        self.authorize_generation(actor, &generation, input.temporary)
            .await?;
        let sources: Vec<EvidenceSource> = parse(
            generation
                .data
                .get("evidence")
                .cloned()
                .context("Missing recovery provenance")?,
        )?;
        ensure!(
            self.evidence_visible(actor, &sources, false).await?,
            "Recovery sources are no longer available"
        );
        let value = self
            .agent_state(actor, &generation, "checkpoint", None)
            .await?;
        let checkpoint =
            value.as_str().context("No checkpoint is available")?;
        let state: Value = serde_json::from_str(checkpoint)?;
        ensure!(
            state.get("safe").and_then(Value::as_bool) == Some(true),
            "An interrupted tool requires review before recovery"
        );
        let original: SendInput = parse(
            generation
                .data
                .get("input")
                .cloned()
                .context("Missing original scope")?,
        )?;
        ensure!(
            input.model_alias == original.model_alias,
            "Recovery requires the original model"
        );
        input.agent_ids = original.agent_ids;
        input.file_ids = original.file_ids;
        input.graph_context = original.graph_context;
        input.web_search = original.web_search;
        input.with_library = original.with_library;
        input.web_query = None;
        input.share_with_isolated_reviewers =
            original.share_with_isolated_reviewers;
        if original.execution_mode.as_deref() == Some("research") ||
            input.execution_mode.is_none()
        {
            input.execution_mode = original.execution_mode;
        }
        Ok(Some(checkpoint.to_owned()))
    }
}

impl PengoloService {
    #[tracing::instrument(skip_all)]
    pub async fn conversation_memory(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        id: Option<&str>,
    ) -> Result<Value> {
        let input: SendInput = parse(
            generation
                .data
                .get("input")
                .cloned()
                .context("Missing generation input")?,
        )?;
        ensure!(
            !input.temporary && generation.owner_id == actor.user,
            "Conversation memory unavailable"
        );
        let current =
            self.authorize_generation(actor, generation, false).await?;
        if let Some(id) = id {
            let chat = self.chat(actor, id, false).await?;
            ensure!(
                chat.project_id == current.project_id &&
                    (chat.project_id.is_some() ||
                        chat.owner_id == actor.user),
                "Conversation memory scope changed"
            );
            let messages = chat
                .data
                .get("messages")
                .and_then(Value::as_array)
                .context("Invalid conversation history")?;
            let mut bytes = 0;
            let selected: Vec<Value> = messages
                .iter()
                .rev()
                .filter(|message| {
                    message
                        .get("status")
                        .and_then(Value::as_str)
                        .is_none_or(|status| status == "completed")
                })
                .take(64)
                .filter_map(|message| {
                    let content = message.get("text")?.as_str()?;
                    if content.len() > 32768 || bytes + content.len() > 196608
                    {
                        return None;
                    }
                    bytes += content.len();
                    Some(json!({"role":message.get("role"),"content":content}))
                })
                .collect();
            let mut evidence = vec![EvidenceSource {
                resource_type: "pengolo_chat".into(),
                resource_id: chat.id.clone(),
                manage: false,
                entity_id: None,
                modality: None,
                state_type: None,
            }];
            for message in messages {
                let sources: Vec<EvidenceSource> = serde_json::from_value(
                    message
                        .get("evidence")
                        .cloned()
                        .unwrap_or_else(|| json!([])),
                )
                .context("Invalid conversation provenance")?;
                evidence.extend(sources);
            }
            self.store
                .record_evidence(
                    actor.tenant,
                    &generation.id,
                    false,
                    &evidence,
                )
                .await?;
            return Ok(
                json!({"messages":selected.into_iter().rev().collect::<Vec<_>>()}),
            );
        }
        let chats = self
            .store
            .list_scoped(
                crate::application::ports::pengolo_store::RecordQuery {
                    tenant: actor.tenant,
                    collection: Collection::Chat,
                    project: current.project_id.as_deref(),
                    owner: actor.user,
                    search: "",
                    after: None,
                    limit: 100,
                },
            )
            .await?;
        let mut visible = Vec::new();
        for chat in chats {
            if chat.id != current.id &&
                self.authorize_record(actor, &chat).await.is_ok()
            {
                visible.push(json!(chat.id));
            }
        }
        tracing::info!(
            event.name = "pengolo.conversation_memory.read",
            conversations = visible.len()
        );
        Ok(json!(visible))
    }
}
