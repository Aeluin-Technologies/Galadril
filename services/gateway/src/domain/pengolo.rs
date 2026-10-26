//! Pengolo contracts keep document scope separate from authorized graph tools.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const MAX_FILE_BYTES: i64 = 25 * 1024 * 1024;
pub const STAGING_TTL_MS: i64 = 60 * 60 * 1000;
pub const NORMAL_TTL_MS: i64 = 30 * 24 * 60 * 60 * 1000;
pub const TEMPORARY_TTL_MS: i64 = 24 * 60 * 60 * 1000;

/// Bounds preferences before they enter provider context or durable storage.
pub fn validate_instructions(value: &str) -> Result<()> {
    ensure!(
        value.chars().count() <= 2000 && value.len() <= 8000 &&
            !value
                .chars()
                .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t')),
        "Invalid Pengolo instructions"
    );
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreferencesInput {
    pub revision: i64,
    pub instructions: String,
}

impl PreferencesInput {
    pub fn parse(value: Value) -> Result<Self> {
        let input: Self = serde_json::from_value(value)?;
        ensure!(input.revision >= 0, "Invalid preferences revision");
        validate_instructions(&input.instructions)?;
        Ok(input)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct GraphContext {
    pub kind: String,
    pub id: String,
}

impl GraphContext {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            matches!(self.kind.as_str(), "entity" | "event" | "relation"),
            "Unsupported graph context kind"
        );
        ensure!(
            !self.id.trim().is_empty() && self.id.len() <= 256,
            "Invalid graph context identifier"
        );
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct SendInput {
    pub idempotency_key: String,
    pub chat_id: Option<String>,
    pub project_id: Option<String>,
    pub prompt: String,
    pub model_alias: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub agent_ids: Vec<String>,
    #[serde(default)]
    pub file_ids: Vec<String>,
    pub graph_context: Option<GraphContext>,
    pub web_query: Option<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub web_search: bool,
    #[serde(default)]
    pub temporary: bool,
    #[serde(default)]
    pub with_library: bool,
}

impl SendInput {
    pub fn parse(value: Value) -> Result<Self> {
        let input: Self = serde_json::from_value(value)
            .map_err(|_| anyhow::anyhow!("Invalid Pengolo send"))?;
        ensure!(
            input.agent_ids.len() <= 8 &&
                input.agent_ids.iter().all(|id| valid_record_id(id)) &&
                input
                    .agent_ids
                    .iter()
                    .collect::<std::collections::HashSet<_>>()
                    .len() ==
                    input.agent_ids.len(),
            "Invalid agent selection"
        );
        uuid::Uuid::parse_str(&input.idempotency_key)
            .map_err(|_| anyhow::anyhow!("Invalid idempotency key"))?;
        ensure!(
            !input.prompt.trim().is_empty() && input.prompt.len() <= 32768,
            "A bounded prompt is required"
        );
        ensure!(
            !input.model_alias.is_empty() && input.model_alias.len() <= 128,
            "A model alias is required"
        );
        ensure!(
            input.file_ids.len() <= 8 &&
                input.file_ids.iter().all(|id| id.len() == 32),
            "Invalid attachments"
        );
        ensure!(
            !input.temporary || input.project_id.is_none(),
            "Temporary chats cannot belong to projects"
        );
        if let Some(context) = &input.graph_context {
            context.validate()?;
        }
        ensure!(
            input
                .web_query
                .as_ref()
                .is_none_or(
                    |query| !query.trim().is_empty() && query.len() <= 512
                ),
            "Invalid public web query"
        );
        Ok(input)
    }
}

fn valid_record_id(id: &str) -> bool {
    id.len() == 32 &&
        id.bytes().all(|byte| {
            byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()
        })
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct AgentInput {
    pub id: Option<String>,
    pub revision: Option<i64>,
    pub name: String,
    pub mission: String,
    pub project_id: Option<String>,
    #[serde(default)]
    pub skill_ids: Vec<String>,
}

impl AgentInput {
    pub fn parse(value: Value) -> Result<Self> {
        ensure!(
            serde_json::to_vec(&value)?.len() <= 16384,
            "Agent exceeds limit"
        );
        let input: Self = serde_json::from_value(value)
            .map_err(|_| anyhow::anyhow!("Invalid agent"))?;
        ensure!(
            input.id.as_deref().is_none_or(valid_record_id) &&
                input.project_id.as_deref().is_none_or(valid_record_id),
            "Invalid agent scope"
        );
        ensure!(
            input.id.is_some() == input.revision.is_some() &&
                input.revision.is_none_or(|revision| revision > 0),
            "Agent revision required for edits"
        );
        ensure!(
            !input.name.trim().is_empty() &&
                input.name.len() <= 128 &&
                !input.mission.trim().is_empty() &&
                input.mission.len() <= 8192,
            "Agent requires a bounded name and mission"
        );
        ensure!(
            input.skill_ids.len() <= 8 &&
                input
                    .skill_ids
                    .iter()
                    .collect::<std::collections::HashSet<_>>()
                    .len() ==
                    input.skill_ids.len(),
            "Invalid Skill selection"
        );
        for id in &input.skill_ids {
            ensure!(
                id.len() <= 128 &&
                    id.contains('.') &&
                    id.split('.').all(|segment| segment
                        .as_bytes()
                        .first()
                        .is_some_and(u8::is_ascii_lowercase) &&
                        segment
                            .bytes()
                            .all(|byte| byte.is_ascii_lowercase() ||
                                byte.is_ascii_digit() ||
                                matches!(byte, b'_' | b'-'))),
                "Invalid Skill identifier"
            );
        }
        Ok(input)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct ProjectInput {
    pub id: Option<String>,
    pub revision: Option<i64>,
    pub name: String,
    pub icon: String,
    pub color: String,
    #[serde(default)]
    pub shared: bool,
    #[serde(default)]
    pub roles: Vec<String>,
    #[serde(default = "project_context")]
    pub context_mode: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub instructions: Option<String>,
}

fn project_context() -> String {
    "project".into()
}

impl ProjectInput {
    pub fn parse(value: Value) -> Result<Self> {
        let input: Self = serde_json::from_value(value)
            .map_err(|_| anyhow::anyhow!("Invalid project"))?;
        if let Some(instructions) = &input.instructions {
            validate_instructions(instructions)?;
        }
        ensure!(
            !input.name.trim().is_empty() &&
                input.name.len() <= 128 &&
                !input.icon.is_empty() &&
                input.icon.len() <= 32,
            "Invalid project name or icon"
        );
        ensure!(
            input.color.len() == 7 &&
                input.color.starts_with('#') &&
                input.color.get(1..).is_some_and(|color| color
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit())),
            "Invalid project color"
        );
        ensure!(
            matches!(input.context_mode.as_str(), "personal" | "project"),
            "Invalid document context"
        );
        ensure!(
            !input.shared ||
                (input.context_mode == "project" &&
                    !input.roles.is_empty()),
            "Shared projects require their own library and roles"
        );
        ensure!(
            input.shared || input.roles.is_empty(),
            "Personal projects cannot have shared roles"
        );
        ensure!(
            input.roles.len() <= 32 &&
                input
                    .roles
                    .iter()
                    .all(|role| !role.is_empty() && role.len() <= 128),
            "Invalid project roles"
        );
        Ok(input)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Record {
    pub id: String,
    pub owner_id: String,
    pub project_id: Option<String>,
    pub expires_at: Option<i64>,
    pub revision: i64,
    pub updated_at: i64,
    pub data: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutputEvent {
    pub sequence: i32,
    pub kind: String,
    pub content: String,
}

impl OutputEvent {
    /// Rejects unrecognized tool names before persisting model-visible
    /// progress.
    pub fn validate_tool_progress(&self) -> Result<()> {
        if self.kind == "activity" {
            super::agent_activity::AgentActivity::parse(&self.content)?;
        }
        if self.kind == "tool" {
            ensure!(
                matches!(
                    self.content.as_str(),
                    "context" |
                        "web" |
                        "search" |
                        "graph" |
                        "causal" |
                        "library" |
                        "read_file" |
                        "document_templates" |
                        "compile_document" |
                        "document_job" |
                        "document_search" |
                        "run_python" |
                        "mcp"
                ),
                "Invalid tool progress"
            );
        }
        Ok(())
    }
}

/// Document reuse never changes an object's security scope implicitly.
pub fn document_scope_allows(
    chat_project: Option<&str>,
    actor: &str,
    temporary_chat: Option<&str>,
    with_library: bool,
    file_project: Option<&str>,
    file_owner: &str,
    temporary_file: Option<&str>,
) -> bool {
    if let Some(file_session) = temporary_file {
        return temporary_chat == Some(file_session) && file_owner == actor;
    }
    if temporary_chat.is_some() {
        return with_library && file_project.is_none() && file_owner == actor;
    }
    match chat_project {
        Some(project) => file_project == Some(project),
        None => file_project.is_none() && file_owner == actor,
    }
}

pub fn file_available(status: &str, expires_at: i64, now: i64) -> bool {
    status == "ready" && expires_at > now
}

pub fn validate_upload(
    name: &str,
    mime: &str,
    size: i64,
) -> Result<&'static str> {
    ensure!(
        !name.trim().is_empty() &&
            name.len() <= 256 &&
            !name.chars().any(char::is_control) &&
            !name.contains(['/', '\\']) &&
            !matches!(name, "." | ".."),
        "Invalid file name"
    );
    ensure!(
        (1..=MAX_FILE_BYTES).contains(&size),
        "File size exceeds the upload limit"
    );
    match mime {
        "application/pdf" => Ok("pdf"),
        "application/zip" => Ok("zip"),
        "text/plain" => Ok("txt"),
        "text/csv" => Ok("csv"),
        "image/png" => Ok("png"),
        "image/jpeg" => Ok("jpg"),
        "image/webp" => Ok("webp"),
        _ => anyhow::bail!("Unsupported file format"),
    }
}

/// Signature constraints do not replace a bounded check of actual object
/// bytes.
pub fn validate_file_prefix(mime: &str, bytes: &[u8]) -> Result<()> {
    let valid = match mime {
        "application/pdf" => bytes.starts_with(b"%PDF-"),
        "application/zip" => {
            bytes.starts_with(b"PK\x03\x04") ||
                bytes.starts_with(b"PK\x05\x06")
        },
        "image/png" => bytes.starts_with(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg" => bytes.starts_with(b"\xff\xd8\xff"),
        "image/webp" => {
            bytes.starts_with(b"RIFF") &&
                bytes.get(8..12) == Some(b"WEBP".as_slice())
        },
        "text/plain" | "text/csv" => {
            !bytes.is_empty() &&
                !bytes.contains(&0) &&
                std::str::from_utf8(bytes).map_or_else(
                    |error| error.error_len().is_none(),
                    |_| true,
                )
        },
        _ => false,
    };
    ensure!(valid, "File content does not match its format");
    Ok(())
}

/// Human names are scope-local labels; opaque object identifiers never change.
pub fn available_file_name(
    name: &str,
    occupied: &std::collections::HashSet<String>,
) -> Result<String> {
    if !occupied.contains(&name.to_lowercase()) {
        return Ok(name.into());
    }
    let (stem, extension) = match name.rsplit_once('.') {
        Some((stem, extension)) if !stem.is_empty() => {
            (stem, format!(".{extension}"))
        },
        _ => (name, String::new()),
    };
    for index in 1..10000 {
        let suffix = format!(" ({index}){extension}");
        ensure!(suffix.len() < 256, "File name exceeds limit");
        let budget = 256 - suffix.len();
        let end = if stem.len() <= budget {
            stem.len()
        } else {
            stem.char_indices()
                .map(|(index, _)| index)
                .take_while(|index| *index <= budget)
                .last()
                .unwrap_or(0)
        };
        let prefix = stem.get(..end).context("Invalid file name")?;
        let candidate = format!("{prefix}{suffix}");
        if !occupied.contains(&candidate.to_lowercase()) {
            return Ok(candidate);
        }
    }
    anyhow::bail!("File name collision limit reached")
}

#[cfg(test)]
mod tests {
    #[test]
    fn personal_instructions_are_bounded_and_cannot_select_another_owner()
    -> Result<()> {
        let input = super::PreferencesInput::parse(
            serde_json::json!({"revision":0,"instructions":"Réponds en français.\nSois concis."}),
        )?;
        assert_eq!(input.revision, 0);
        for value in [
            serde_json::json!({"revision":-1,"instructions":""}),
            serde_json::json!({"revision":0,"instructions":"x".repeat(2001)}),
            serde_json::json!({"revision":0,"instructions":"bad\u{0000}"}),
            serde_json::json!({"revision":0,"instructions":"","owner":"other"}),
        ] {
            assert!(super::PreferencesInput::parse(value).is_err());
        }
        Ok(())
    }

    #[test]
    fn projects_accept_optional_instructions_without_silently_clearing_them()
    -> Result<()> {
        let mut value = serde_json::json!({"name":"Case","icon":"folder","color":"#d97706"});
        assert!(ProjectInput::parse(value.clone())?.instructions.is_none());
        value["instructions"] = serde_json::json!("Use a formal tone.");
        assert_eq!(
            ProjectInput::parse(value.clone())?.instructions.as_deref(),
            Some("Use a formal tone.")
        );
        value["instructions"] = serde_json::json!("x".repeat(2001));
        assert!(ProjectInput::parse(value).is_err());
        Ok(())
    }
    #[test]
    fn specialized_agents_require_unique_bounded_skills_and_explicit_scope()
    -> anyhow::Result<()> {
        let value = serde_json::json!({"name":"Administrative case law", "mission":"Find and verify French administrative court decisions", "projectId":null, "skillIds":["law.skill.research"]});
        assert!(super::AgentInput::parse(value.clone()).is_ok());
        for patch in [
            serde_json::json!({"mission":" "}),
            serde_json::json!({"skillIds":["law.skill.research","law.skill.research"]}),
            serde_json::json!({"skillIds":["not-namespaced"]}),
            serde_json::json!({"implementationId":"law.agent.privileged"}),
            serde_json::json!({"projectId":"invalid"}),
        ] {
            let mut invalid = value.clone();
            for (key, value) in patch
                .as_object()
                .ok_or_else(|| anyhow::anyhow!("Invalid fixture"))?
            {
                invalid
                    .as_object_mut()
                    .ok_or_else(|| anyhow::anyhow!("Invalid fixture"))?
                    .insert(key.clone(), value.clone());
            }
            assert!(super::AgentInput::parse(invalid).is_err());
        }
        Ok(())
    }
    use super::*;

    #[test]
    fn document_read_progress_is_accepted_without_allowing_unknown_tools()
    -> Result<()> {
        for operation in ["context", "library", "read_file", "document_search"]
        {
            OutputEvent {
                sequence: 1,
                kind: "tool".into(),
                content: operation.into(),
            }
            .validate_tool_progress()?;
        }
        assert!(
            OutputEvent {
                sequence: 1,
                kind: "tool".into(),
                content: "untrusted_tool".into()
            }
            .validate_tool_progress()
            .is_err()
        );
        Ok(())
    }

    #[test]
    fn duplicate_display_names_preserve_extensions_without_paths() -> Result<()>
    {
        let names = std::collections::HashSet::from([
            "report.pdf".into(),
            "report (1).pdf".into(),
        ]);
        assert_eq!(
            available_file_name("report.pdf", &names)?,
            "report (2).pdf"
        );
        assert!(
            validate_upload("../report.pdf", "application/pdf", 12).is_err()
        );
        assert!(
            validate_upload("folder\\report.pdf", "application/pdf", 12)
                .is_err()
        );
        Ok(())
    }

    #[test]
    fn shared_projects_cannot_select_personal_context() {
        let value = serde_json::json!({"name":"Operations","icon":"📊","color":"#d97706","shared":true,"roles":["analyst"],"contextMode":"personal"});
        assert!(ProjectInput::parse(value).is_err());
    }

    #[test]
    fn web_search_opt_in_preserves_legacy_idempotency() -> Result<()> {
        let input = SendInput::parse(
            serde_json::json!({"idempotencyKey":"12345678-1234-1234-1234-123456789abc","prompt":"hello","modelAlias":"local"}),
        )?;
        assert!(!input.web_search);
        assert!(serde_json::to_value(&input)?.get("webSearch").is_none());
        let enabled = SendInput {
            web_search: true,
            ..input
        };
        assert_eq!(
            serde_json::to_value(&enabled)?.get("webSearch"),
            Some(&serde_json::json!(true))
        );
        Ok(())
    }

    #[test]
    fn pengolo_rejects_reasoning_overrides_and_empty_sends() -> Result<()> {
        let base = serde_json::json!({"idempotencyKey":"12345678-1234-1234-1234-123456789abc","prompt":"hello","modelAlias":"local"});
        assert!(SendInput::parse(base.clone()).is_ok());
        let mut changed = base.clone();
        changed
            .as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("Missing object"))?
            .insert("reasoningEffort".into(), serde_json::json!("high"));
        assert!(SendInput::parse(changed).is_err());
        let mut empty = base;
        empty
            .as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("Missing object"))?
            .insert("prompt".into(), serde_json::json!(" "));
        assert!(SendInput::parse(empty).is_err());
        Ok(())
    }

    #[test]
    fn temporary_without_library_cannot_reuse_persistent_files() {
        assert!(!document_scope_allows(
            None,
            "u",
            Some("t"),
            false,
            None,
            "u",
            None
        ));
        assert!(document_scope_allows(
            None,
            "u",
            Some("t"),
            true,
            None,
            "u",
            None
        ));
        assert!(!document_scope_allows(
            Some("a"),
            "u",
            None,
            false,
            Some("b"),
            "u",
            None
        ));
        assert!(!document_scope_allows(
            Some("a"),
            "u",
            None,
            false,
            None,
            "u",
            None
        ));
        assert!(document_scope_allows(
            Some("a"),
            "u",
            None,
            false,
            Some("a"),
            "other",
            None
        ));
    }

    #[test]
    fn upload_validation_and_expiration_fail_closed() {
        assert!(
            validate_upload("invoice.pdf", "application/pdf", 1024).is_ok()
        );
        assert!(validate_upload("payload.html", "text/html", 1024).is_err());
        assert!(validate_upload("file.pdf", "application/pdf", 0).is_err());
        assert!(
            validate_upload("file.pdf", "application/pdf", MAX_FILE_BYTES + 1)
                .is_err()
        );
        assert!(!file_available("ready", 100, 100));
        assert!(!file_available("uploading", 101, 100));
        assert!(file_available("ready", 101, 100));
    }
}
