//! Loads published definitions from immutable tenant TerminusDB snapshots.

use anyhow::{Context, Result};
use galadril_versioning::{TerminusClient, TerminusConfig, named};
use serde_json::Value;

use crate::domain::ports::{
    PipelineCatalog, PipelineIdentity, PublishedPipeline,
    validate_pipeline_tenant,
};

pub struct TerminusPipelineCatalog {
    client: TerminusClient,
}

impl TerminusPipelineCatalog {
    pub fn new(config: TerminusConfig) -> Result<Self> {
        Ok(Self {
            client: TerminusClient::new(config)?,
        })
    }
}

#[async_trait::async_trait]
impl PipelineCatalog for TerminusPipelineCatalog {
    fn authorize_tenant(&self, tenant_id: &str) -> Result<()> {
        validate_pipeline_tenant(tenant_id)?;
        self.client.path(tenant_id, "main", false)?;
        Ok(())
    }

    async fn published(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<PublishedPipeline>> {
        self.authorize_tenant(tenant_id)?;
        let snapshot = self.client.read(tenant_id, "main", false).await?;
        let mut definitions = Vec::new();
        for document in &snapshot.documents {
            if document
                .get("deleted_at_ms")
                .is_some_and(|value| !value.is_null())
            {
                continue;
            }
            let Some(revision) = document
                .get("published_revision_id")
                .and_then(Value::as_str)
            else {
                continue;
            };
            let pipeline = document
                .get("pipeline_id")
                .and_then(Value::as_str)
                .context("Invalid published pipeline")?;
            let published =
                self.client.read(tenant_id, revision, true).await?;
            let entry =
                named(&published.documents, &format!("pipeline/{pipeline}"))
                    .context("Published pipeline snapshot is unavailable")?;
            definitions.push(PublishedPipeline {
                identity: PipelineIdentity::new(
                    tenant_id, pipeline, revision,
                )?,
                definition: serde_json::to_string(
                    entry
                        .get("definition")
                        .context("Published definition is unavailable")?,
                )?,
            });
        }
        Ok(definitions)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tenant_boundary_rejects_paths_and_empty_context() {
        for tenant in [
            "",
            " tenant_a",
            "tenant_a/tenant_b",
            "tenant_a%2Ftenant_b",
            "..",
        ] {
            assert!(validate_pipeline_tenant(tenant).is_err());
        }
        assert!(validate_pipeline_tenant("tenant_A-1").is_ok());
    }
}
