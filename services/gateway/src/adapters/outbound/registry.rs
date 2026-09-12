//! Registry gRPC adapter with PostgreSQL retained only for execution records.

use anyhow::{Context, Result};
use galadril_registry::grpc::RegistryClient;
use galadril_registry::proto;
use serde_json::Value;
use sqlx::Row;
use tonic::Code;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::control_plane_store::{
    ControlPlaneStore, OntologyCatalogEntry, OntologyPublication,
    PipelineExecution, PipelineOntologyBinding,
};
use crate::application::ports::pipeline_store::{
    NewPipelineRevision, PipelineDefinition, PipelineStore,
};

const HARD_LIMIT: usize = 100;

pub struct RegistryStore {
    client: RegistryClient,
    database: Database,
}

impl RegistryStore {
    /// Composes remote artifact persistence with local audit/execution reads.
    pub fn new(client: RegistryClient, database: Database) -> Self {
        Self { client, database }
    }

    /// Converts a Registry pipeline message into the Gateway domain model.
    fn pipeline(value: proto::Pipeline) -> Result<PipelineDefinition> {
        Ok(PipelineDefinition {
            pipeline_id: value.pipeline_id,
            name: value.name,
            owner_id: value.owner_id,
            head_revision_id: value.head_revision_id,
            published_revision_id: value.published_revision_id,
            definition: serde_json::from_slice(&value.definition_json)?,
            author_id: value.author_id,
            message: value.message,
            created_at_ms: value.created_at_ms,
            updated_at_ms: value.updated_at_ms,
            deleted_at_ms: value.deleted_at_ms,
        })
    }

    /// Converts Registry failures without exposing transport internals
    /// upstream.
    fn request_error(status: tonic::Status) -> anyhow::Error {
        anyhow::anyhow!("Registry request failed: {}", status.message())
    }

    /// Converts PostgreSQL timestamps to the API's Unix-millisecond form.
    fn to_ms(value: sqlx::types::time::OffsetDateTime) -> i64 {
        value.unix_timestamp() * 1000 +
            i64::from(value.nanosecond()) / 1_000_000
    }

    /// Converts a Registry ontology publication into the Gateway projection.
    fn publication(
        value: proto::OntologyPublication,
    ) -> Result<OntologyPublication> {
        Ok(OntologyPublication {
            publication_id: value.publication_id,
            revision_id: value.revision_id,
            lifecycle: value.lifecycle,
            metadata: serde_json::from_slice(&value.metadata_json)?,
            base_version: value.base_version,
            base_hash: value.base_hash,
            effective_hash: value.effective_hash,
            author: value.author,
            message: value.message,
            published_at_ms: value.published_at_ms,
            retired_at_ms: value.retired_at_ms,
        })
    }
}

#[async_trait::async_trait]
impl PipelineStore for RegistryStore {
    async fn create(
        &self,
        tenant: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let mut client = self.client.clone();
        let response = client
            .put_pipeline(proto::PutPipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: revision.pipeline_id.to_owned(),
                name: revision.name.to_owned(),
                owner_id: revision.owner_id.to_owned(),
                definition_json: serde_json::to_vec(revision.definition)?,
                author_id: revision.author_id.to_owned(),
                message: revision.message.to_owned(),
                expected_revision_id: None,
                create_only: true,
            })
            .await
            .map_err(Self::request_error)?;
        Self::pipeline(response.into_inner())
    }

    async fn update(
        &self,
        tenant: &str,
        expected: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let mut client = self.client.clone();
        let response = client
            .put_pipeline(proto::PutPipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: revision.pipeline_id.to_owned(),
                name: revision.name.to_owned(),
                owner_id: revision.owner_id.to_owned(),
                definition_json: serde_json::to_vec(revision.definition)?,
                author_id: revision.author_id.to_owned(),
                message: revision.message.to_owned(),
                expected_revision_id: Some(expected.to_owned()),
                create_only: false,
            })
            .await
            .map_err(Self::request_error)?;
        Self::pipeline(response.into_inner())
    }

    async fn list(
        &self,
        tenant: &str,
        limit: usize,
    ) -> Result<Vec<PipelineDefinition>> {
        let mut client = self.client.clone();
        client
            .list_pipelines(proto::ListPipelinesRequest {
                tenant_id: tenant.to_owned(),
                limit: u32::try_from(limit.min(HARD_LIMIT))?,
            })
            .await
            .map_err(Self::request_error)?
            .into_inner()
            .pipelines
            .into_iter()
            .map(Self::pipeline)
            .collect()
    }

    async fn get(
        &self,
        tenant: &str,
        pipeline_id: &str,
    ) -> Result<Option<PipelineDefinition>> {
        let mut client = self.client.clone();
        match client
            .get_pipeline(proto::GetPipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: pipeline_id.to_owned(),
                revision_id: None,
            })
            .await
        {
            Ok(response) => Self::pipeline(response.into_inner()).map(Some),
            Err(status) if status.code() == Code::NotFound => Ok(None),
            Err(status) => Err(Self::request_error(status)),
        }
    }

    async fn publish(
        &self,
        tenant: &str,
        pipeline_id: &str,
        revision: &str,
    ) -> Result<PipelineDefinition> {
        let mut client = self.client.clone();
        let response = client
            .publish_pipeline(proto::PublishPipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: pipeline_id.to_owned(),
                revision_id: revision.to_owned(),
            })
            .await
            .map_err(Self::request_error)?;
        Self::pipeline(response.into_inner())
    }

    async fn delete(
        &self,
        tenant: &str,
        pipeline_id: &str,
        expected: &str,
    ) -> Result<()> {
        let mut client = self.client.clone();
        client
            .delete_pipeline(proto::DeletePipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: pipeline_id.to_owned(),
                expected_revision_id: expected.to_owned(),
            })
            .await
            .map_err(Self::request_error)?;
        Ok(())
    }
}

#[async_trait::async_trait]
impl ControlPlaneStore for RegistryStore {
    async fn publish_ontology(
        &self,
        tenant: &str,
        ontology_id: &str,
        display_name: &str,
        publication_id: &str,
        revision_id: &str,
        metadata: &Value,
        create_only: bool,
    ) -> Result<OntologyPublication> {
        let mut client = self.client.clone();
        let response = client
            .publish_ontology(proto::PublishOntologyRequest {
                tenant_id: tenant.to_owned(),
                ontology_id: ontology_id.to_owned(),
                display_name: display_name.to_owned(),
                publication_id: publication_id.to_owned(),
                revision_id: revision_id.to_owned(),
                metadata_json: serde_json::to_vec(metadata)?,
                create_only,
            })
            .await
            .map_err(Self::request_error)?;
        Self::publication(response.into_inner())
    }

    async fn retire_ontology(
        &self,
        tenant: &str,
        ontology_id: &str,
        publication_id: &str,
        revision_id: &str,
    ) -> Result<()> {
        let mut client = self.client.clone();
        client
            .retire_ontology(proto::RetireOntologyRequest {
                tenant_id: tenant.to_owned(),
                ontology_id: ontology_id.to_owned(),
                publication_id: publication_id.to_owned(),
                revision_id: revision_id.to_owned(),
            })
            .await
            .map_err(Self::request_error)?;
        Ok(())
    }

    async fn ontology_exists(
        &self,
        tenant: &str,
        ontology_id: &str,
    ) -> Result<bool> {
        let mut client = self.client.clone();
        match client
            .get_ontology(proto::GetOntologyRequest {
                tenant_id: tenant.to_owned(),
                ontology_id: ontology_id.to_owned(),
                revision_id: String::new(),
            })
            .await
        {
            Ok(_) => Ok(true),
            Err(status) if status.code() == Code::NotFound => Ok(false),
            Err(status) => Err(Self::request_error(status)),
        }
    }

    async fn list_ontologies(
        &self,
        tenant: &str,
        limit: usize,
    ) -> Result<Vec<OntologyCatalogEntry>> {
        let mut client = self.client.clone();
        client
            .list_ontologies(proto::ListOntologiesRequest {
                tenant_id: tenant.to_owned(),
                limit: u32::try_from(limit.min(HARD_LIMIT))?,
            })
            .await
            .map_err(Self::request_error)?
            .into_inner()
            .ontologies
            .into_iter()
            .map(|entry| {
                Ok(OntologyCatalogEntry {
                    ontology_id: entry.ontology_id,
                    display_name: entry.display_name,
                    production_publication: entry
                        .production_publication
                        .map(Self::publication)
                        .transpose()?,
                })
            })
            .collect()
    }

    async fn list_ontology_bindings(
        &self,
        tenant: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>> {
        let mut client = self.client.clone();
        client
            .list_bindings(proto::ListBindingsRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: pipeline_id.map(str::to_owned),
                limit: u32::try_from(limit.min(HARD_LIMIT))?,
                revision_id: None,
            })
            .await
            .map_err(Self::request_error)?
            .into_inner()
            .bindings
            .into_iter()
            .map(|binding| {
                Ok(PipelineOntologyBinding {
                    pipeline_id: binding.pipeline_id,
                    block_id: binding.block_id,
                    ontology_id: binding.ontology_id,
                    resource_ids: serde_json::to_value(binding.resource_ids)?,
                    resource_kinds: serde_json::to_value(
                        binding.resource_kinds,
                    )?,
                    include_dependencies: binding.include_dependencies,
                    metadata: serde_json::from_slice(&binding.metadata_json)?,
                    updated_at_ms: binding.updated_at_ms,
                })
            })
            .collect()
    }

    async fn list_pipeline_executions(
        &self,
        tenant: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineExecution>> {
        let mut tx = self.database.tenant(tenant).await?;
        let rows = sqlx::query(
            r#"
            SELECT idempotency_key, command_id, correlation_id, pipeline,
                   step, status, attempt, lease_expires_at, result, error,
                   created_at, updated_at
            FROM pipeline_executions
            WHERE tenant_id = $1
              AND ($2::text IS NULL OR pipeline = $2)
            ORDER BY updated_at DESC, idempotency_key DESC
            LIMIT $3
            "#,
        )
        .bind(tenant)
        .bind(pipeline_id)
        .bind(i64::try_from(limit.clamp(1, HARD_LIMIT))?)
        .fetch_all(&mut *tx)
        .await
        .context("Failed to list pipeline execution audit records")?;
        tx.commit()
            .await
            .context("Failed to commit pipeline execution audit read")?;
        let mut executions = Vec::with_capacity(rows.len());
        for row in rows {
            let lease_expires_at: sqlx::types::time::OffsetDateTime =
                row.try_get("lease_expires_at")?;
            let created_at: sqlx::types::time::OffsetDateTime =
                row.try_get("created_at")?;
            let updated_at: sqlx::types::time::OffsetDateTime =
                row.try_get("updated_at")?;
            executions.push(PipelineExecution {
                idempotency_key: row.try_get("idempotency_key")?,
                command_id: row.try_get("command_id")?,
                correlation_id: row.try_get("correlation_id")?,
                pipeline_id: row.try_get("pipeline")?,
                step: row.try_get("step")?,
                status: row.try_get("status")?,
                attempt: row.try_get("attempt")?,
                lease_expires_at_ms: Self::to_ms(lease_expires_at),
                result: row.try_get("result")?,
                error: row.try_get("error")?,
                created_at_ms: Self::to_ms(created_at),
                updated_at_ms: Self::to_ms(updated_at),
            });
        }
        Ok(executions)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pipeline_conversion_preserves_opaque_registry_revisions() -> Result<()>
    {
        let pipeline = RegistryStore::pipeline(proto::Pipeline {
            pipeline_id: "daily".to_owned(),
            name: "Daily".to_owned(),
            owner_id: "alice".to_owned(),
            head_revision_id: "opaque-revision-id".to_owned(),
            published_revision_id: Some("published-commit".to_owned()),
            definition_json: serde_json::to_vec(&serde_json::json!({}))?,
            author_id: "alice".to_owned(),
            message: "Edit".to_owned(),
            created_at_ms: 1,
            updated_at_ms: 2,
            deleted_at_ms: None,
        })?;
        assert_eq!(pipeline.head_revision_id, "opaque-revision-id");
        assert_eq!(
            pipeline.published_revision_id.as_deref(),
            Some("published-commit")
        );
        Ok(())
    }
}
