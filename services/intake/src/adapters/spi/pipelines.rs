//! Loads published definitions through the typed Registry gRPC boundary.

use anyhow::{Context, Result, ensure};
use galadril_registry::grpc::RegistryClient;
use galadril_registry::proto;

use crate::domain::ports::{
    PipelineCatalog, PipelineIdentity, PublishedPipeline,
    validate_pipeline_tenant,
};

/// Tenant-scoped pipeline catalogue backed exclusively by Registry.
pub struct RegistryPipelineCatalog {
    client: RegistryClient,
}

impl RegistryPipelineCatalog {
    /// Creates a catalogue that validates each requested tenant with Registry.
    pub fn new(client: RegistryClient) -> Self {
        Self { client }
    }
}

#[async_trait::async_trait]
impl PipelineCatalog for RegistryPipelineCatalog {
    fn authorize_tenant(&self, tenant_id: &str) -> Result<()> {
        validate_pipeline_tenant(tenant_id)?;
        Ok(())
    }

    async fn published(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<PublishedPipeline>> {
        self.authorize_tenant(tenant_id)?;
        let mut client = self.client.clone();
        let validation = client
            .validate_tenants(proto::ValidateTenantsRequest {
                tenant_ids: vec![tenant_id.to_owned()],
            })
            .await
            .context("Registry tenant validation failed")?
            .into_inner();
        ensure!(
            validation.tenants.first().is_some_and(
                |tenant| tenant.exists && tenant.tenant_id == tenant_id
            ),
            "Pipeline tenant is unavailable"
        );
        let pipelines = client
            .list_published_pipelines(proto::ListPublishedPipelinesRequest {
                tenant_id: tenant_id.to_owned(),
                limit: 100,
            })
            .await
            .context("Registry published pipeline request failed")?
            .into_inner()
            .pipelines;
        let mut definitions = Vec::with_capacity(pipelines.len());
        for pipeline in pipelines {
            let identity = PipelineIdentity::new(
                tenant_id,
                &pipeline.pipeline_id,
                &pipeline.head_revision_id,
            )?;
            let definition = String::from_utf8(pipeline.definition_json)
                .context("Registry returned invalid pipeline JSON encoding")?;
            definitions.push(PublishedPipeline {
                identity,
                definition,
            });
        }
        Ok(definitions)
    }
}

#[cfg(test)]
mod tests {
    use galadril_registry::grpc::registry_client;

    use super::*;

    #[tokio::test]
    async fn tenant_boundary_rejects_paths_and_untrusted_context() -> Result<()>
    {
        let client = registry_client("http://127.0.0.1:50052")?;
        let catalog = RegistryPipelineCatalog::new(client);

        for tenant in [
            "",
            " tenant_a",
            "tenant_a/tenant_b",
            "tenant_a%2Ftenant_b",
            "..",
        ] {
            assert!(catalog.authorize_tenant(tenant).is_err());
        }
        assert!(catalog.authorize_tenant("tenant_A-1").is_ok());
        assert!(catalog.authorize_tenant("tenant_b").is_ok());
        Ok(())
    }
}
