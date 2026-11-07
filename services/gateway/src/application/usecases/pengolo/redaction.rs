//! Native redaction keeps source bytes outside model context and publishes
//! generated copies through the authorized Pengolo artifact lifecycle.

use super::*;
use crate::application::ports::conversation_agent::DocumentOperation;
use crate::domain::redaction::{RedactionInput, RedactionResult};

impl PengoloService {
    #[tracing::instrument(skip_all)]
    pub async fn native_redaction(
        &self,
        actor: &Principal<'_>,
        input: RedactionInput,
    ) -> Result<RedactionResult> {
        self.verify(actor).await?;
        input.validate()?;
        let value = self
            .agent
            .document_operation(
                DocumentOperation::Redact,
                serde_json::to_value(&input)?,
            )
            .await?;
        let result: RedactionResult = serde_json::from_value(value)
            .context("Invalid native redaction response")?;
        drop(result.validate(&input.mime)?);
        self.verify(actor).await?;
        tracing::info!(
            event.name = "pengolo.redaction.completed",
            matches = result.match_count,
            pages = result.pages
        );
        Ok(result)
    }
}

impl PengoloService {
    #[tracing::instrument(skip_all)]
    pub async fn redact_attached_document(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        file_id: &str,
        rules: Vec<crate::domain::redaction::RedactionRule>,
        request_id: &str,
    ) -> Result<Value> {
        use sha2::{Digest, Sha256};

        ensure!(
            (1..=16).contains(&rules.len()),
            "Invalid redaction rule count"
        );
        ensure!(
            !request_id.is_empty() && request_id.len() <= 256,
            "Invalid redaction request"
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
            input.file_ids.iter().any(|id| id == file_id),
            "Redaction requires an explicitly attached file"
        );
        let file = self.attached_file(actor, &input, file_id).await?;
        let mime = string(&file.data, "mime")?;
        ensure!(
            matches!(mime, "application/pdf" | "text/plain"),
            "Unsupported redaction document"
        );
        let mut chunks = self.objects.read(string(&file.data, "key")?).await?;
        let mut bytes = Vec::new();
        while let Some(chunk) = chunks.next().await {
            let chunk = chunk?;
            ensure!(
                bytes.len() + chunk.len() <=
                    crate::domain::redaction::MAX_DOCUMENT_BYTES,
                "Redaction document exceeds limit"
            );
            bytes.extend_from_slice(&chunk);
        }
        let checksum = base64::engine::general_purpose::STANDARD
            .encode(Sha256::digest(&bytes));
        ensure!(
            file.data.get("checksum").and_then(Value::as_str) ==
                Some(checksum.as_str()),
            "Document checksum changed"
        );
        let request = RedactionInput {
            mime: mime.into(),
            content_base64: base64::engine::general_purpose::STANDARD
                .encode(bytes),
            rules,
        };
        let result = self.native_redaction(actor, request).await?;
        let bytes = result.validate(mime)?;
        self.authorize_generation(actor, generation, input.temporary)
            .await?;
        let current = self.attached_file(actor, &input, file_id).await?;
        ensure!(
            current.project_id == file.project_id &&
                current.data.get("checksum") == file.data.get("checksum"),
            "Redaction source changed"
        );
        let identity = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&(
                generation.id.as_str(),
                file_id,
                request_id
            ))?)
        );
        let filename = if mime == "application/pdf" {
            "redacted.pdf"
        } else {
            "redacted.txt"
        };
        let artifact = self
            .save_generated_artifact(
                actor,
                generation,
                super::compiler::CompiledArtifact {
                    id: &identity,
                    name: filename,
                    mime,
                    bytes,
                },
            )
            .await?;
        Ok(json!({"artifact":artifact,"matchCount":result.match_count,
            "coverage":result.coverage,"requiresReview":result.requires_review,
            "instruction":"Provide the artifact download link. This copy covers the supplied rules and requires review."}))
    }
}
