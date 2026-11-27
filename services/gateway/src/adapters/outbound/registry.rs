//! Registry gRPC adapter with PostgreSQL retained only for execution records.

use anyhow::{Context, Result};
use galadril_registry::grpc::RegistryClient;
use galadril_registry::proto;
use sqlx::Row;
use tonic::Code;

use crate::adapters::outbound::database::connection::Database;
use crate::application::ports::control_plane_store::{
    ControlPlaneStore, GolCompileApproval, GolDependencyPreview,
    GolDiagnostic, GolFile, GolPackageOrigin, GolPublication, GolSummary,
    GolValidation, GolWorkspace, GolWrite, PipelineExecution,
    PipelineOntologyBinding,
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
            authoring: if value.authoring_json.is_empty() {
                Default::default()
            } else {
                serde_json::from_slice(&value.authoring_json)?
            },
            previous_revision_id: value.previous_revision_id,
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

    fn gol_summary(value: proto::GolSummary) -> GolSummary {
        GolSummary {
            ontology_id: value.ontology_id,
            display_name: value.display_name,
            source_revision: value.source_revision,
            active_generation: value.active_generation,
            active_source_revision: value.active_source_revision,
            status: value.status,
            origin: value.origin.map(|origin| GolPackageOrigin {
                plugin_id: origin.plugin_id,
                plugin_version: origin.plugin_version,
                package_version: origin.package_version,
            }),
        }
    }

    fn gol_diagnostic(value: proto::GolDiagnostic) -> Result<GolDiagnostic> {
        Ok(GolDiagnostic {
            code: value.code,
            message: value.message,
            path: value.path,
            start: usize::try_from(value.start)?,
            end: usize::try_from(value.end)?,
            severity: value.severity,
        })
    }

    fn gol_dependency(
        value: proto::GolDependency,
    ) -> galadril_registry::gol_service::GolDependency {
        galadril_registry::gol_service::GolDependency {
            ontology_id: value.ontology_id,
            generation: value.generation,
            source_revision: value.source_revision,
        }
    }

    fn gol_workspace(value: proto::GolWorkspace) -> Result<GolWorkspace> {
        Ok(GolWorkspace {
            summary: Self::gol_summary(
                value.summary.context("Registry GOL summary missing")?,
            ),
            files: value
                .files
                .into_iter()
                .map(|file| GolFile {
                    path: file.path,
                    text: file.text,
                })
                .collect(),
            diagnostics: value
                .diagnostics
                .into_iter()
                .map(Self::gol_diagnostic)
                .collect::<Result<Vec<_>>>()?,
            dependencies: value
                .dependencies
                .into_iter()
                .map(Self::gol_dependency)
                .collect(),
        })
    }
}

#[async_trait::async_trait]
impl PipelineStore for RegistryStore {
    async fn author(
        &self,
        tenant: &str,
        expected: Option<&str>,
        revision: &NewPipelineRevision<'_>,
        command: &galadril_registry::authoring::AuthoringCommand,
    ) -> Result<PipelineDefinition> {
        let response = self
            .client
            .clone()
            .put_pipeline(proto::PutPipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: revision.pipeline_id.to_owned(),
                name: revision.name.to_owned(),
                owner_id: revision.owner_id.to_owned(),
                definition_json: serde_json::to_vec(revision.definition)?,
                author_id: revision.author_id.to_owned(),
                message: revision.message.to_owned(),
                expected_revision_id: expected.map(str::to_owned),
                create_only: expected.is_none(),
                authoring_command_json: Some(serde_json::to_vec(command)?),
            })
            .await
            .map_err(Self::request_error)?;
        Self::pipeline(response.into_inner())
    }

    async fn create(
        &self,
        tenant: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let mut client = self.client.clone();
        let response = client
            .put_pipeline(proto::PutPipelineRequest {
                authoring_command_json: None,
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
                authoring_command_json: None,
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

    async fn get_revision(
        &self,
        tenant: &str,
        pipeline_id: &str,
        revision_id: &str,
    ) -> Result<Option<PipelineDefinition>> {
        let mut client = self.client.clone();
        match client
            .get_pipeline(proto::GetPipelineRequest {
                tenant_id: tenant.to_owned(),
                pipeline_id: pipeline_id.to_owned(),
                revision_id: Some(revision_id.to_owned()),
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
    async fn gol_workspace(
        &self,
        tenant: &str,
        id: &str,
    ) -> Result<GolWorkspace> {
        let value = self
            .client
            .clone()
            .get_gol_workspace(proto::GetGolWorkspaceRequest {
                tenant_id: tenant.into(),
                ontology_id: id.into(),
                source_revision: None,
            })
            .await
            .map_err(Self::request_error)?
            .into_inner();
        Self::gol_workspace(value)
    }

    async fn save_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        input: GolWrite<'_>,
    ) -> Result<GolWorkspace> {
        let value = self
            .client
            .clone()
            .save_gol_ontology(proto::SaveGolOntologyRequest {
                tenant_id: tenant.into(),
                ontology_id: input.ontology_id.into(),
                display_name: input.display_name.into(),
                files: input
                    .files
                    .iter()
                    .map(|file| proto::GolFile {
                        path: file.path.clone(),
                        text: file.text.clone(),
                    })
                    .collect(),
                author: user.into(),
                expected_revision: input.expected_revision.map(str::to_owned),
                origin: input.origin.map(|origin| proto::GolPackageOrigin {
                    plugin_id: origin.plugin_id,
                    plugin_version: origin.plugin_version,
                    package_version: origin.package_version,
                }),
            })
            .await
            .map_err(Self::request_error)?
            .into_inner();
        Self::gol_workspace(value)
    }

    async fn preview_gol_dependencies(
        &self,
        tenant: &str,
        id: &str,
        revision: &str,
        approved: &[String],
    ) -> Result<GolDependencyPreview> {
        let value = self
            .client
            .clone()
            .preview_gol_dependencies(proto::PreviewGolDependenciesRequest {
                tenant_id: tenant.into(),
                ontology_id: id.into(),
                source_revision: revision.into(),
                approved_owners: approved.to_vec(),
            })
            .await
            .map_err(Self::request_error)?
            .into_inner();
        Ok(GolDependencyPreview {
            required_owners: value.required_owners,
            plan_pin: value.plan_pin,
            dependencies: value
                .dependencies
                .into_iter()
                .map(Self::gol_dependency)
                .collect(),
        })
    }

    async fn validate_gol_ontology(
        &self,
        tenant: &str,
        id: &str,
        revision: &str,
        approval: &GolCompileApproval,
    ) -> Result<GolValidation> {
        let value = self
            .client
            .clone()
            .validate_gol_ontology(proto::ValidateGolOntologyRequest {
                tenant_id: tenant.into(),
                ontology_id: id.into(),
                source_revision: revision.into(),
                plan_pin: approval.plan_pin.clone(),
                approved_owners: approval.approved_owners.clone(),
            })
            .await
            .map_err(Self::request_error)?
            .into_inner();
        Ok(GolValidation {
            ontology_id: value.ontology_id,
            source_revision: value.source_revision,
            status: value.status,
            diagnostics: value
                .diagnostics
                .into_iter()
                .map(Self::gol_diagnostic)
                .collect::<Result<Vec<_>>>()?,
            dependencies: value
                .dependencies
                .into_iter()
                .map(Self::gol_dependency)
                .collect(),
        })
    }

    async fn publish_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        id: &str,
        revision: &str,
        expected: Option<&str>,
        approval: &GolCompileApproval,
    ) -> Result<GolPublication> {
        let value = self
            .client
            .clone()
            .publish_gol_ontology(proto::PublishGolOntologyRequest {
                tenant_id: tenant.into(),
                ontology_id: id.into(),
                source_revision: revision.into(),
                author_id: user.into(),
                expected_generation: expected.map(str::to_owned),
                plan_pin: approval.plan_pin.clone(),
                approved_owners: approval.approved_owners.clone(),
            })
            .await
            .map_err(Self::request_error)?
            .into_inner();
        Ok(GolPublication {
            ontology_id: value.ontology_id,
            source_revision: value.source_revision,
            generation: value.generation,
        })
    }

    async fn delete_gol_ontology(
        &self,
        tenant: &str,
        user: &str,
        id: &str,
        expected: &str,
    ) -> Result<String> {
        Ok(self
            .client
            .clone()
            .delete_gol_ontology(proto::DeleteGolOntologyRequest {
                tenant_id: tenant.into(),
                ontology_id: id.into(),
                expected_revision: expected.into(),
                author: user.into(),
            })
            .await
            .map_err(Self::request_error)?
            .into_inner()
            .revision)
    }

    async fn list_gol_ontologies(
        &self,
        tenant: &str,
        limit: usize,
    ) -> Result<Vec<GolSummary>> {
        Ok(self
            .client
            .clone()
            .list_gol_ontologies(proto::ListGolOntologiesRequest {
                tenant_id: tenant.into(),
                limit: u32::try_from(limit.min(HARD_LIMIT))?,
            })
            .await
            .map_err(Self::request_error)?
            .into_inner()
            .ontologies
            .into_iter()
            .map(Self::gol_summary)
            .collect())
    }

    async fn bind_ontology(
        &self,
        tenant: &str,
        user: &str,
        pipeline: &str,
        block: &str,
        ontology: &str,
        expected: &str,
    ) -> Result<String> {
        let current = PipelineStore::get(self, tenant, pipeline)
            .await?
            .context("Pipeline unavailable")?;
        anyhow::ensure!(
            current.head_revision_id == expected,
            "pipeline head changed"
        );
        let saved = PipelineStore::author(
            self,
            tenant,
            Some(expected),
            &NewPipelineRevision {
                pipeline_id: pipeline,
                name: &current.name,
                owner_id: &current.owner_id,
                definition: &current.definition,
                author_id: user,
                message: "Bind tenant-selected ontology",
            },
            &galadril_registry::authoring::AuthoringCommand::BindOntology {
                block_id: block.into(),
                ontology_id: ontology.into(),
            },
        )
        .await?;
        Ok(saved.head_revision_id)
    }

    async fn list_ontology_bindings(
        &self,
        tenant: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>> {
        Ok(self
            .client
            .clone()
            .list_bindings(proto::ListBindingsRequest {
                tenant_id: tenant.into(),
                pipeline_id: pipeline_id.map(str::to_owned),
                revision_id: None,
                limit: u32::try_from(limit.min(HARD_LIMIT))?,
            })
            .await
            .map_err(Self::request_error)?
            .into_inner()
            .bindings
            .into_iter()
            .map(|binding| PipelineOntologyBinding {
                pipeline_id: binding.pipeline_id,
                block_id: binding.block_id,
                ontology_id: binding.ontology_id,
                generation: binding.generation,
                source_revision: binding.source_revision,
                symbols: binding.symbols,
                updated_at_ms: binding.updated_at_ms,
            })
            .collect())
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
            authoring_json: Vec::new(),
            previous_revision_id: None,
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
