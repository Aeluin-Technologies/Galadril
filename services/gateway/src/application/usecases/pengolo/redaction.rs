//! Native private processing has no chat, indexing or object-store side
//! effects.

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
