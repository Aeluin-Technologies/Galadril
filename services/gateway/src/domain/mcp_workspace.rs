//! OAuth scopes restrict operations before live tenant and resource IAM
//! checks.

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const CAPABILITY: &str = "core.mcp.workspace";

pub fn resource() -> String {
    std::env::var("MCP_WORKSPACE_RESOURCE")
        .unwrap_or_else(|_| "https://mcp.gravitalia.com/workspace/mcp".into())
}

/// Native mediation cannot broaden the tool selection of confidential
/// conversations.
pub fn generation_operation_allowed(
    operation: &str,
    specialized: bool,
    library: bool,
) -> bool {
    match operation {
        "document_templates" | "compile_document" | "document_job" |
        "redact_document" | "read_file" | "document_search" => {
            library || specialized
        },
        "web" | "email" => true,
        "search" | "evidence" | "graph" | "causal" | "context" => !specialized,
        _ => false,
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceRequest {
    pub project_id: Option<String>,
    pub request: Value,
}

/// Exact operation allowlists keep account administration and approval outside
/// model authority.
pub fn required_scope(request: &Value) -> Result<&'static str> {
    match request.get("operation").and_then(Value::as_str) {
        Some("email") => match request
            .pointer("/request/operation")
            .and_then(Value::as_str)
        {
            Some(
                "accounts" | "contacts" | "messages" | "message" | "thread" |
                "attachment" | "drafts" | "draft" | "proposal_status",
            ) => Ok("mail:read"),
            Some("save_draft") => Ok("mail:draft"),
            _ => bail!("MCP email operation denied"),
        },
        Some("email_send") => Ok("mail:send"),
        Some("email_save_draft") => Ok("mail:draft"),
        Some("search" | "graph" | "causal" | "evidence") => Ok("data:read"),
        Some("document_templates") => match request
            .pointer("/request/action")
            .and_then(Value::as_str)
            .context("Missing template action")?
        {
            "list" | "read" | "plugin_catalog" => Ok("documents:read"),
            "put" | "publish" | "delete" | "export" | "document_case" => {
                Ok("documents:write")
            },
            _ => bail!("MCP template operation denied"),
        },
        Some("request_document_signature") => Ok("documents:read"),
        Some("compile_document" | "document_job") => Ok("documents:write"),
        _ => bail!("MCP operation denied"),
    }
}

/// New clients share one write scope, while old draft grants cannot request
/// delivery.
pub fn grants_scope(scopes: &[String], required: &str) -> bool {
    let unified = match required {
        "mail:read" => Some("email:read"),
        "mail:draft" | "mail:send" => Some("email:write"),
        _ => None,
    };
    scopes
        .iter()
        .any(|scope| scope == required || unified == Some(scope.as_str()))
}

/// Stable, length-delimited identities let jobs survive MCP reconnects without
/// crossing principals.
pub fn execution_id(
    tenant: &str,
    user: &str,
    client: &str,
    project: Option<&str>,
) -> String {
    let mut hash = Sha256::new();
    for field in [
        "mcp-workspace-v1",
        tenant,
        user,
        client,
        project.unwrap_or(""),
    ] {
        hash.update((field.len() as u64).to_be_bytes());
        hash.update(field.as_bytes());
    }
    format!("{:x}", hash.finalize())
}

/// Attachment bytes can contain vault-bound identities; tools receive metadata
/// only.
pub fn draft_projection(mut value: Value) -> Value {
    if let Some(items) = value.as_array_mut() {
        for item in items {
            *item = draft_projection(item.take());
        }
    } else if let Some(files) = value
        .pointer_mut("/content/attachments")
        .and_then(Value::as_array_mut)
    {
        for file in files {
            if let Some(fields) = file.as_object_mut() &&
                let Some(content) = fields.remove("content")
            {
                fields.insert(
                    "encoded_size".into(),
                    Value::from(content.as_str().map_or(0, str::len)),
                );
            }
        }
    }
    value
}

#[derive(Debug, PartialEq, Eq)]
pub enum EmailDelivery {
    Send,
    Review,
    Sent,
}

/// Only the reviewed revision is eligible for delivery. In-flight and
/// uncertain sends must be reconciled by the mail service.
pub fn email_delivery(
    draft: &Value,
    revision: i64,
    require_review: bool,
) -> Result<EmailDelivery> {
    anyhow::ensure!(
        draft.get("revision").and_then(Value::as_i64) == Some(revision),
        "Email revision changed"
    );
    match draft.get("status").and_then(Value::as_str) {
        Some("sent") => Ok(EmailDelivery::Sent),
        Some("draft" | "approved") if require_review => {
            Ok(EmailDelivery::Review)
        },
        Some("draft" | "approved") => Ok(EmailDelivery::Send),
        _ => bail!("Email delivery unavailable"),
    }
}
