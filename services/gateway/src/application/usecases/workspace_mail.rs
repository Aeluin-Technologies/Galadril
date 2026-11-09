//! Compiled attachments travel between trusted services, never through model
//! arguments.

use base64::Engine as _;
use futures::StreamExt as _;
use sha2::{Digest, Sha256};

use super::*;

const MAX_ATTACHMENTS_BYTES: usize = 8 * 1024 * 1024;

impl ChatTools {
    pub(super) async fn workspace_save_draft(
        &self,
        identity: &WorkspaceIdentity,
        payload: Value,
    ) -> Result<Value> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Save {
            operation: String,
            request: Value,
            #[serde(default)]
            file_ids: Vec<String>,
        }
        let mut input: Save = serde_json::from_value(payload)?;
        ensure!(
            input.operation == "email_save_draft" &&
                input.request.get("operation").and_then(Value::as_str) ==
                    Some("save_draft"),
            "Invalid draft operation"
        );
        ensure!(
            input.file_ids.len() <= 16 &&
                input
                    .file_ids
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len() ==
                    input.file_ids.len(),
            "Invalid attachment selection"
        );
        let actor = Principal {
            tenant: &identity.tenant,
            user: &identity.user,
            context: &identity.context,
        };
        if !input.file_ids.is_empty() {
            ensure!(
                identity
                    .scopes
                    .iter()
                    .any(|scope| scope == "documents:read"),
                "MCP document scope denied"
            );
            self.attach_workspace_files(
                &actor,
                &mut input.request,
                &input.file_ids,
            )
            .await?;
        }
        let result = self
            .email
            .get()
            .context("Email tools unavailable")?
            .operation(actor.tenant, actor.user, &input.request, true)
            .await?;
        Ok(crate::domain::mcp_workspace::draft_projection(result))
    }

    async fn attach_workspace_files(
        &self,
        actor: &Principal<'_>,
        request: &mut Value,
        ids: &[String],
    ) -> Result<()> {
        let content = request
            .get_mut("content")
            .and_then(Value::as_object_mut)
            .context("Draft content required")?;
        let attachments = content
            .entry("attachments")
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .context("Invalid attachments")?;
        ensure!(attachments.len() + ids.len() <= 16, "Too many attachments");
        let service = self.pengolo_service()?;
        let mut total = 0_usize;
        for id in ids {
            ensure!(
                id.len() == 32 &&
                    id.bytes().all(|byte| byte.is_ascii_hexdigit() &&
                        !byte.is_ascii_uppercase()),
                "Invalid attachment identifier"
            );
            let (file, mut chunks) =
                service.file_content(actor, id, None).await?;
            let size = file
                .data
                .get("size")
                .and_then(Value::as_u64)
                .context("Invalid attachment size")?;
            let size = usize::try_from(size)?;
            ensure!(
                size <= MAX_ATTACHMENTS_BYTES.saturating_sub(total),
                "Attachments exceed limit"
            );
            let mut bytes = Vec::with_capacity(size);
            while let Some(chunk) = chunks.next().await {
                let chunk = chunk?;
                ensure!(
                    chunk.len() <=
                        MAX_ATTACHMENTS_BYTES
                            .saturating_sub(total + bytes.len()),
                    "Attachments exceed limit"
                );
                bytes.extend_from_slice(&chunk);
            }
            let checksum = base64::engine::general_purpose::STANDARD
                .encode(Sha256::digest(&bytes));
            ensure!(
                bytes.len() == size &&
                    file.data.get("checksum").and_then(Value::as_str) ==
                        Some(checksum.as_str()),
                "Attachment changed"
            );
            service.download(actor, json!({"id":id})).await?;
            total += bytes.len();
            attachments.push(json!({"name":file.data.get("name"), "content_type":file.data.get("mime"), "content":base64::engine::general_purpose::STANDARD.encode(bytes)}));
        }
        Ok(())
    }
}
