//! Typed gRPC boundary for Registry callers and the service binary.

use std::sync::Arc;

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::Value;
use tonic::transport::{Channel, Endpoint};
use tonic::{Code, Request, Response, Status};

use crate::domain::{
    OntologyView, PipelineView, PutOntology, PutPipeline, Registry, TenantView,
};
use crate::proto;
use crate::state::{OntologyBindingRecord, OntologyPublicationRecord};
use crate::storage::RevisionStore;

/// Cloneable generated client used by trusted internal services.
pub type RegistryClient = proto::registry_client::RegistryClient<Channel>;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
/// Internal caller configuration containing only the Registry endpoint.
pub struct RegistryClientConfig {
    /// Registry gRPC endpoint; storage coordinates are intentionally absent.
    pub endpoint: String,
}

impl RegistryClientConfig {
    /// Creates a typed Registry client from the validated endpoint.
    pub fn connect_lazy(&self) -> Result<RegistryClient> {
        registry_client(&self.endpoint)
    }
}

/// Creates a bounded lazy channel without issuing network I/O at startup.
pub fn registry_client(endpoint: &str) -> Result<RegistryClient> {
    let endpoint = Endpoint::from_shared(endpoint.to_owned())?
        .connect_timeout(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(30));
    Ok(RegistryClient::new(endpoint.connect_lazy()))
}

/// gRPC implementation delegating every mutation to canonical domain
/// semantics.
pub struct RegistryGrpc<S> {
    registry: Arc<Registry<S>>,
}

impl<S> RegistryGrpc<S> {
    /// Shares one Registry domain instance across concurrent gRPC requests.
    pub fn new(registry: Arc<Registry<S>>) -> Self {
        Self { registry }
    }
}

#[tonic::async_trait]
impl<S> proto::registry_server::Registry for RegistryGrpc<S>
where
    S: RevisionStore + 'static,
{
    async fn validate_tenants(
        &self,
        request: Request<proto::ValidateTenantsRequest>,
    ) -> Result<Response<proto::ValidateTenantsResponse>, Status> {
        let request = request.into_inner();
        let existence = self
            .registry
            .validate_tenants(&request.tenant_ids)
            .await
            .map_err(domain_status)?;
        let tenants = request
            .tenant_ids
            .into_iter()
            .zip(existence)
            .map(|(tenant_id, exists)| proto::TenantValidation {
                tenant_id,
                exists,
            })
            .collect();
        Ok(Response::new(proto::ValidateTenantsResponse { tenants }))
    }

    async fn get_tenant(
        &self,
        request: Request<proto::GetTenantRequest>,
    ) -> Result<Response<proto::Tenant>, Status> {
        let request = request.into_inner();
        let tenant = self
            .registry
            .tenant(&request.tenant_id)
            .await
            .map_err(domain_status)?;
        Ok(Response::new(tenant_message(tenant)))
    }

    async fn put_tenant(
        &self,
        request: Request<proto::PutTenantRequest>,
    ) -> Result<Response<proto::Tenant>, Status> {
        let request = request.into_inner();
        let tenant = self
            .registry
            .put_tenant(&request.tenant_id)
            .await
            .map_err(domain_status)?;
        Ok(Response::new(tenant_message(tenant)))
    }

    async fn delete_tenant(
        &self,
        request: Request<proto::DeleteTenantRequest>,
    ) -> Result<Response<proto::DeleteTenantResponse>, Status> {
        let request = request.into_inner();
        self.registry
            .delete_tenant(&request.tenant_id, &request.confirmation_tenant_id)
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::DeleteTenantResponse {}))
    }

    async fn put_pipeline(
        &self,
        request: Request<proto::PutPipelineRequest>,
    ) -> Result<Response<proto::Pipeline>, Status> {
        let request = request.into_inner();
        let definition = json_from_bytes(&request.definition_json)?;
        let view = self
            .registry
            .put_pipeline(PutPipeline {
                tenant_id: &request.tenant_id,
                pipeline_id: &request.pipeline_id,
                name: &request.name,
                owner_id: &request.owner_id,
                definition,
                author: &request.author_id,
                message: &request.message,
                expected_revision_id: request.expected_revision_id.as_deref(),
                create_only: request.create_only,
            })
            .await
            .map_err(domain_status)?;
        Ok(Response::new(pipeline_message(view)?))
    }

    async fn get_pipeline(
        &self,
        request: Request<proto::GetPipelineRequest>,
    ) -> Result<Response<proto::Pipeline>, Status> {
        let request = request.into_inner();
        let view = if let Some(revision) = request.revision_id.as_deref() {
            let record = self
                .registry
                .runtime_pipeline(
                    &request.tenant_id,
                    &request.pipeline_id,
                    revision,
                )
                .await
                .map_err(domain_status)?;
            PipelineView {
                head_revision_id: revision.to_owned(),
                record,
            }
        } else {
            self.registry
                .current_pipeline(&request.tenant_id, &request.pipeline_id)
                .await
                .map_err(domain_status)?
        };
        Ok(Response::new(pipeline_message(view)?))
    }

    async fn list_pipelines(
        &self,
        request: Request<proto::ListPipelinesRequest>,
    ) -> Result<Response<proto::ListPipelinesResponse>, Status> {
        let request = request.into_inner();
        let pipelines = self
            .registry
            .list_pipelines(&request.tenant_id, request.limit as usize)
            .await
            .map_err(domain_status)?
            .into_iter()
            .map(pipeline_message)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Response::new(proto::ListPipelinesResponse { pipelines }))
    }

    async fn publish_pipeline(
        &self,
        request: Request<proto::PublishPipelineRequest>,
    ) -> Result<Response<proto::Pipeline>, Status> {
        let request = request.into_inner();
        let pipeline = self
            .registry
            .publish_pipeline(
                &request.tenant_id,
                &request.pipeline_id,
                &request.revision_id,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(pipeline_message(pipeline)?))
    }

    async fn delete_pipeline(
        &self,
        request: Request<proto::DeletePipelineRequest>,
    ) -> Result<Response<proto::DeletePipelineResponse>, Status> {
        let request = request.into_inner();
        let revision_id = self
            .registry
            .delete_pipeline(
                &request.tenant_id,
                &request.pipeline_id,
                &request.expected_revision_id,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::DeletePipelineResponse { revision_id }))
    }

    async fn list_published_pipelines(
        &self,
        request: Request<proto::ListPublishedPipelinesRequest>,
    ) -> Result<Response<proto::ListPipelinesResponse>, Status> {
        let request = request.into_inner();
        let pipelines = self
            .registry
            .list_published_pipelines(
                &request.tenant_id,
                request.limit as usize,
            )
            .await
            .map_err(domain_status)?
            .into_iter()
            .map(pipeline_message)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Response::new(proto::ListPipelinesResponse { pipelines }))
    }

    async fn get_runtime_pipeline(
        &self,
        request: Request<proto::GetRuntimePipelineRequest>,
    ) -> Result<Response<proto::Pipeline>, Status> {
        let request = request.into_inner();
        let record = self
            .registry
            .runtime_pipeline(
                &request.tenant_id,
                &request.pipeline_id,
                &request.revision_id,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(pipeline_message(PipelineView {
            head_revision_id: request.revision_id,
            record,
        })?))
    }

    async fn put_ontology(
        &self,
        request: Request<proto::PutOntologyRequest>,
    ) -> Result<Response<proto::OntologyRevision>, Status> {
        let request = request.into_inner();
        let ontology = json_from_bytes(&request.ontology_json)?;
        let view = self
            .registry
            .put_ontology(PutOntology {
                tenant_id: &request.tenant_id,
                ontology_id: &request.ontology_id,
                display_name: &request.display_name,
                ontology,
                author: &request.author,
                message: &request.message,
                expected_revision_id: request.expected_revision_id.as_deref(),
                create_only: request.create_only,
            })
            .await
            .map_err(domain_status)?;
        Ok(Response::new(ontology_message(view)?))
    }

    async fn get_ontology(
        &self,
        request: Request<proto::GetOntologyRequest>,
    ) -> Result<Response<proto::OntologyRevision>, Status> {
        let request = request.into_inner();
        let view = if request.revision_id.is_empty() {
            self.registry
                .current_ontology(&request.tenant_id, &request.ontology_id)
                .await
                .map_err(domain_status)?
        } else {
            self.registry
                .ontology_at(
                    &request.tenant_id,
                    &request.ontology_id,
                    &request.revision_id,
                )
                .await
                .map_err(domain_status)?
        };
        Ok(Response::new(ontology_message(view)?))
    }

    async fn publish_ontology(
        &self,
        request: Request<proto::PublishOntologyRequest>,
    ) -> Result<Response<proto::OntologyPublication>, Status> {
        let request = request.into_inner();
        let metadata = json_from_bytes(&request.metadata_json)?;
        let publication = self
            .registry
            .publish_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.display_name,
                &request.publication_id,
                &request.revision_id,
                metadata,
                request.create_only,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(publication_message(
            request.ontology_id,
            request.display_name,
            publication,
        )?))
    }

    async fn retire_ontology(
        &self,
        request: Request<proto::RetireOntologyRequest>,
    ) -> Result<Response<proto::RetireOntologyResponse>, Status> {
        let request = request.into_inner();
        let head_revision_id = self
            .registry
            .retire_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.publication_id,
                &request.revision_id,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::RetireOntologyResponse {
            head_revision_id,
        }))
    }

    async fn list_ontologies(
        &self,
        request: Request<proto::ListOntologiesRequest>,
    ) -> Result<Response<proto::ListOntologiesResponse>, Status> {
        let request = request.into_inner();
        let ontologies = self
            .registry
            .list_ontologies(&request.tenant_id, request.limit as usize)
            .await
            .map_err(domain_status)?
            .into_iter()
            .map(|ontology| {
                let production_publication = ontology
                    .production_publication
                    .map(|publication| {
                        publication_message(
                            ontology.ontology_id.clone(),
                            ontology.display_name.clone(),
                            publication,
                        )
                    })
                    .transpose()?;
                Ok(proto::OntologyCatalogEntry {
                    ontology_id: ontology.ontology_id,
                    display_name: ontology.display_name,
                    production_publication,
                })
            })
            .collect::<Result<Vec<_>, Status>>()?;
        Ok(Response::new(proto::ListOntologiesResponse { ontologies }))
    }

    async fn put_binding(
        &self,
        request: Request<proto::PutBindingRequest>,
    ) -> Result<Response<proto::OntologyBinding>, Status> {
        let request = request.into_inner();
        let binding = request
            .binding
            .context("ontology binding is required")
            .map_err(invalid_status)?;
        let record = binding_record(binding)?;
        let (record, head_revision_id) = self
            .registry
            .put_binding(
                &request.tenant_id,
                &request.expected_revision_id,
                record,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(binding_message(record, head_revision_id)?))
    }

    async fn list_bindings(
        &self,
        request: Request<proto::ListBindingsRequest>,
    ) -> Result<Response<proto::ListBindingsResponse>, Status> {
        let request = request.into_inner();
        let bindings = self
            .registry
            .list_bindings(
                &request.tenant_id,
                request.pipeline_id.as_deref(),
                request.revision_id.as_deref(),
                request.limit as usize,
            )
            .await
            .map_err(domain_status)?
            .into_iter()
            .map(|binding| binding_message(binding, String::new()))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Response::new(proto::ListBindingsResponse { bindings }))
    }

    async fn get_ontology_slice(
        &self,
        request: Request<proto::GetOntologySliceRequest>,
    ) -> Result<Response<proto::OntologySlice>, Status> {
        let request = request.into_inner();
        let slice = self
            .registry
            .ontology_slice(
                &request.tenant_id,
                &request.pipeline_id,
                &request.pipeline_revision_id,
                &request.block_id,
            )
            .await
            .map_err(domain_status)?;
        let ontology_json = json_bytes(&slice.ontology)?;
        let publication_metadata_json =
            json_bytes(&slice.publication.metadata)?;
        let binding_metadata_json = json_bytes(&slice.binding.metadata)?;
        Ok(Response::new(proto::OntologySlice {
            ontology_id: slice.binding.ontology_id,
            publication_id: slice.publication.publication_id,
            revision_id: slice.publication.revision_id,
            ontology_json,
            publication_metadata_json,
            binding_metadata_json,
            published_at_ms: slice.publication.published_at_ms,
            base_version: slice.publication.base_version,
            base_hash: slice.publication.base_hash,
            effective_hash: slice.publication.effective_hash,
        }))
    }

    async fn diff_ontology(
        &self,
        request: Request<proto::DiffOntologyRequest>,
    ) -> Result<Response<proto::SemanticDiff>, Status> {
        let request = request.into_inner();
        let changes = self
            .registry
            .diff_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.base_revision_id,
                &request.target_revision_id,
            )
            .await
            .map_err(domain_status)?
            .into_iter()
            .map(|change| {
                Ok(proto::SemanticChange {
                    resource_id: change.resource_id,
                    operation: change.operation,
                    path: change.path,
                    before_json: optional_json_bytes(change.before.as_ref())?,
                    after_json: optional_json_bytes(change.after.as_ref())?,
                })
            })
            .collect::<Result<Vec<_>, Status>>()?;
        Ok(Response::new(proto::SemanticDiff { changes }))
    }

    async fn merge_ontology(
        &self,
        request: Request<proto::MergeOntologyRequest>,
    ) -> Result<Response<proto::OntologyMerge>, Status> {
        let request = request.into_inner();
        let (result, revision) = self
            .registry
            .merge_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.base_revision_id,
                &request.left_revision_id,
                &request.right_revision_id,
                &request.expected_revision_id,
                &request.author,
                &request.message,
            )
            .await
            .map_err(domain_status)?;
        let revision = revision.map(ontology_message).transpose()?;
        let conflicts = result
            .conflicts
            .into_iter()
            .map(|conflict| {
                Ok(proto::MergeConflict {
                    resource_id: conflict.resource_id,
                    path: conflict.path,
                    base_json: optional_json_bytes(conflict.base.as_ref())?,
                    left_json: optional_json_bytes(conflict.left.as_ref())?,
                    right_json: optional_json_bytes(conflict.right.as_ref())?,
                })
            })
            .collect::<Result<Vec<_>, Status>>()?;
        Ok(Response::new(proto::OntologyMerge {
            revision,
            conflicts,
        }))
    }
}

fn tenant_message(view: TenantView) -> proto::Tenant {
    proto::Tenant {
        tenant_id: view.tenant_id,
        head_revision_id: view.head_revision_id,
    }
}

fn pipeline_message(view: PipelineView) -> Result<proto::Pipeline, Status> {
    Ok(proto::Pipeline {
        pipeline_id: view.record.pipeline_id,
        name: view.record.name,
        owner_id: view.record.owner_id,
        head_revision_id: view.head_revision_id,
        published_revision_id: view.record.published_revision_id,
        definition_json: json_bytes(&view.record.definition)?,
        author_id: view.record.author_id,
        message: view.record.message,
        created_at_ms: view.record.created_at_ms,
        updated_at_ms: view.record.updated_at_ms,
        deleted_at_ms: view.record.deleted_at_ms,
    })
}

fn ontology_message(
    view: OntologyView,
) -> Result<proto::OntologyRevision, Status> {
    Ok(proto::OntologyRevision {
        ontology_id: view.record.ontology_id,
        display_name: view.record.display_name,
        revision_id: view.revision_id,
        ontology_json: json_bytes(&view.record.ontology)?,
        author: view.record.author,
        message: view.record.message,
        created_at_ms: view.record.created_at_ms,
    })
}

fn publication_message(
    ontology_id: String,
    display_name: String,
    publication: OntologyPublicationRecord,
) -> Result<proto::OntologyPublication, Status> {
    Ok(proto::OntologyPublication {
        ontology_id,
        display_name,
        publication_id: publication.publication_id,
        revision_id: publication.revision_id,
        lifecycle: publication.lifecycle,
        metadata_json: json_bytes(&publication.metadata)?,
        base_version: publication.base_version,
        base_hash: publication.base_hash,
        effective_hash: publication.effective_hash,
        author: publication.author,
        message: publication.message,
        published_at_ms: publication.published_at_ms,
        retired_at_ms: publication.retired_at_ms,
    })
}

fn binding_record(
    binding: proto::OntologyBinding,
) -> Result<OntologyBindingRecord, Status> {
    Ok(OntologyBindingRecord {
        pipeline_id: binding.pipeline_id,
        block_id: binding.block_id,
        ontology_id: binding.ontology_id,
        resource_ids: binding.resource_ids,
        resource_kinds: binding.resource_kinds,
        include_dependencies: binding.include_dependencies,
        metadata: json_from_bytes(&binding.metadata_json)?,
        updated_at_ms: binding.updated_at_ms,
    })
}

fn binding_message(
    binding: OntologyBindingRecord,
    head_revision_id: String,
) -> Result<proto::OntologyBinding, Status> {
    Ok(proto::OntologyBinding {
        pipeline_id: binding.pipeline_id,
        block_id: binding.block_id,
        ontology_id: binding.ontology_id,
        resource_ids: binding.resource_ids,
        resource_kinds: binding.resource_kinds,
        include_dependencies: binding.include_dependencies,
        metadata_json: json_bytes(&binding.metadata)?,
        updated_at_ms: binding.updated_at_ms,
        head_revision_id,
    })
}

fn json_from_bytes(payload: &[u8]) -> Result<Value, Status> {
    if payload.len() > 16 * 1024 * 1024 {
        return Err(Status::invalid_argument(
            "JSON payload exceeds safety limit",
        ));
    }
    serde_json::from_slice(payload).map_err(invalid_status)
}

fn json_bytes(value: &Value) -> Result<Vec<u8>, Status> {
    serde_json::to_vec(value).map_err(internal_status)
}

fn optional_json_bytes(value: Option<&Value>) -> Result<Vec<u8>, Status> {
    value.map_or_else(|| Ok(Vec::new()), json_bytes)
}

fn domain_status(error: anyhow::Error) -> Status {
    let message = error.to_string();
    if message.contains("integrity check failed") ||
        message.contains("invalid Registry state artifact")
    {
        tracing::error!("registry_persisted_state_integrity_failed");
        return Status::data_loss(
            "Registry persisted state failed validation",
        );
    }
    if message.contains("S3") ||
        message.contains("lakeFS") ||
        message.contains("lock poisoned")
    {
        tracing::error!("registry_storage_operation_failed");
        return Status::unavailable("Registry storage is unavailable");
    }
    let code = if message.contains("changed") {
        Code::Aborted
    } else if message.contains("unavailable") || message.contains("missing") {
        Code::NotFound
    } else {
        Code::InvalidArgument
    };
    Status::new(code, message)
}

fn invalid_status(error: impl std::fmt::Display) -> Status {
    Status::invalid_argument(error.to_string())
}

fn internal_status(error: impl std::fmt::Display) -> Status {
    tracing::error!(error = %error, "registry_response_serialization_failed");
    Status::internal("Registry response serialization failed")
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    use anyhow::{Context, ensure};
    use async_trait::async_trait;

    use super::*;
    use crate::proto::registry_server::Registry as RegistryService;
    use crate::state::TenantState;
    use crate::storage::{Snapshot, TenantSnapshot};

    #[derive(Default)]
    struct MemoryStore {
        tenants: Mutex<BTreeMap<String, Vec<StoredSnapshot>>>,
    }

    struct StoredSnapshot {
        revision_id: String,
        state_json: Vec<u8>,
    }

    #[async_trait]
    impl RevisionStore for MemoryStore {
        async fn tenant_exists(
            &self,
            tenant_id: &str,
        ) -> anyhow::Result<bool> {
            let tenants = self
                .tenants
                .lock()
                .map_err(|_| anyhow::anyhow!("memory store lock poisoned"))?;
            Ok(tenants.contains_key(tenant_id))
        }

        async fn ensure_tenant(
            &self,
            tenant_id: &str,
        ) -> anyhow::Result<TenantSnapshot> {
            let mut tenants = self
                .tenants
                .lock()
                .map_err(|_| anyhow::anyhow!("memory store lock poisoned"))?;
            let snapshots = tenants.entry(tenant_id.to_owned()).or_default();
            Ok(TenantSnapshot {
                revision_id: snapshots.last().map_or_else(
                    || "00000000000000000000".to_owned(),
                    |snapshot| snapshot.revision_id.clone(),
                ),
            })
        }

        async fn delete_tenant(&self, tenant_id: &str) -> anyhow::Result<()> {
            let mut tenants = self
                .tenants
                .lock()
                .map_err(|_| anyhow::anyhow!("memory store lock poisoned"))?;
            tenants.remove(tenant_id).context("tenant unavailable")?;
            Ok(())
        }

        async fn load(
            &self,
            tenant_id: &str,
            revision_id: Option<&str>,
        ) -> anyhow::Result<Snapshot> {
            let tenants = self
                .tenants
                .lock()
                .map_err(|_| anyhow::anyhow!("memory store lock poisoned"))?;
            let snapshots =
                tenants.get(tenant_id).context("tenant unavailable")?;
            let stored = if let Some(revision_id) = revision_id {
                Some(
                    snapshots
                        .iter()
                        .find(|item| item.revision_id == revision_id)
                        .context("revision unavailable")?,
                )
            } else {
                snapshots.last()
            };
            let Some(stored) = stored else {
                return Ok(Snapshot {
                    revision_id: "00000000000000000000".to_owned(),
                    state: TenantState::default(),
                });
            };
            Ok(Snapshot {
                revision_id: stored.revision_id.clone(),
                state: serde_json::from_slice(&stored.state_json)?,
            })
        }

        async fn commit(
            &self,
            tenant_id: &str,
            expected_revision_id: &str,
            state: &TenantState,
            _author: &str,
            _message: &str,
        ) -> anyhow::Result<String> {
            let mut tenants = self
                .tenants
                .lock()
                .map_err(|_| anyhow::anyhow!("memory store lock poisoned"))?;
            let snapshots = tenants.entry(tenant_id.to_owned()).or_default();
            let current =
                snapshots.last().map_or("00000000000000000000", |snapshot| {
                    snapshot.revision_id.as_str()
                });
            ensure!(
                current == expected_revision_id,
                "registry revision changed"
            );
            let revision_id = format!("{:020}", snapshots.len() + 1);
            snapshots.push(StoredSnapshot {
                revision_id: revision_id.clone(),
                state_json: serde_json::to_vec(state)?,
            });
            Ok(revision_id)
        }
    }

    fn ontology(description: &str) -> Vec<u8> {
        serde_json::json!({
            "version": "1",
            "resources": [{
                "resource_id": "object.person",
                "kind": "object_type",
                "display_name": "Person",
                "description": description,
                "references": [],
                "attributes": {}
            }]
        })
        .to_string()
        .into_bytes()
    }

    fn pipeline() -> Vec<u8> {
        serde_json::json!({
            "name": "daily",
            "sources": [{"id": "source"}],
            "pipeline": [{
                "step": "sink",
                "type": "sink",
                "input_from": ["source"],
                "params": {"ontology_id": "people"}
            }]
        })
        .to_string()
        .into_bytes()
    }

    fn put_ontology_request(
        description: &str,
        expected_revision_id: Option<String>,
        create_only: bool,
    ) -> proto::PutOntologyRequest {
        proto::PutOntologyRequest {
            tenant_id: "tenant_a".to_owned(),
            ontology_id: "people".to_owned(),
            display_name: "People".to_owned(),
            ontology_json: ontology(description),
            author: "author".to_owned(),
            message: format!("ontology {description}"),
            expected_revision_id,
            create_only,
        }
    }

    #[tokio::test]
    async fn tenant_contract_validates_initializes_reads_and_purges()
    -> anyhow::Result<()> {
        let service =
            RegistryGrpc::new(Arc::new(Registry::new(MemoryStore::default())));

        let validation = service
            .validate_tenants(Request::new(proto::ValidateTenantsRequest {
                tenant_ids: vec!["tenant_a".to_owned()],
            }))
            .await?
            .into_inner();
        assert_eq!(validation.tenants.len(), 1);
        assert!(!validation.tenants.first().context("tenant result")?.exists);

        let Err(empty) = service
            .validate_tenants(Request::new(proto::ValidateTenantsRequest {
                tenant_ids: Vec::new(),
            }))
            .await
        else {
            anyhow::bail!("empty tenant validation exposed tenant discovery");
        };
        assert_eq!(empty.code(), Code::InvalidArgument);

        let Err(missing) = service
            .get_tenant(Request::new(proto::GetTenantRequest {
                tenant_id: "tenant_a".to_owned(),
            }))
            .await
        else {
            anyhow::bail!("missing tenant accepted");
        };
        assert_eq!(missing.code(), Code::NotFound);

        let tenant = service
            .put_tenant(Request::new(proto::PutTenantRequest {
                tenant_id: "tenant_a".to_owned(),
            }))
            .await?
            .into_inner();
        assert_eq!(tenant.tenant_id, "tenant_a");
        assert!(crate::storage::valid_revision(&tenant.head_revision_id));
        let Err(mismatch) = service
            .delete_tenant(Request::new(proto::DeleteTenantRequest {
                tenant_id: "tenant_a".to_owned(),
                confirmation_tenant_id: "tenant_b".to_owned(),
            }))
            .await
        else {
            anyhow::bail!("mismatched purge confirmation accepted");
        };
        assert_eq!(mismatch.code(), Code::InvalidArgument);

        service
            .delete_tenant(Request::new(proto::DeleteTenantRequest {
                tenant_id: "tenant_a".to_owned(),
                confirmation_tenant_id: "tenant_a".to_owned(),
            }))
            .await?;
        let validation = service
            .validate_tenants(Request::new(proto::ValidateTenantsRequest {
                tenant_ids: vec!["tenant_a".to_owned()],
            }))
            .await?
            .into_inner();
        assert!(!validation.tenants.first().context("tenant result")?.exists);
        Ok(())
    }

    #[tokio::test]
    async fn grpc_round_trip_covers_the_complete_registry_contract()
    -> anyhow::Result<()> {
        let service =
            RegistryGrpc::new(Arc::new(Registry::new(MemoryStore::default())));

        let base = service
            .put_ontology(Request::new(put_ontology_request(
                "base", None, true,
            )))
            .await?
            .into_inner();
        let left = service
            .put_ontology(Request::new(put_ontology_request(
                "left",
                Some(base.revision_id.clone()),
                false,
            )))
            .await?
            .into_inner();
        let right = service
            .put_ontology(Request::new(put_ontology_request(
                "right",
                Some(left.revision_id.clone()),
                false,
            )))
            .await?
            .into_inner();

        let current = service
            .get_ontology(Request::new(proto::GetOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                ontology_id: "people".to_owned(),
                revision_id: String::new(),
            }))
            .await?
            .into_inner();
        assert_eq!(current.revision_id, right.revision_id);
        let pinned = service
            .get_ontology(Request::new(proto::GetOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                ontology_id: "people".to_owned(),
                revision_id: base.revision_id.clone(),
            }))
            .await?
            .into_inner();
        assert_eq!(pinned.ontology_json, ontology("base"));

        let diff = service
            .diff_ontology(Request::new(proto::DiffOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                base_revision_id: base.revision_id.clone(),
                target_revision_id: left.revision_id.clone(),
                ontology_id: "people".to_owned(),
            }))
            .await?
            .into_inner();
        assert!(!diff.changes.is_empty());

        let conflict = service
            .merge_ontology(Request::new(proto::MergeOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                ontology_id: "people".to_owned(),
                base_revision_id: base.revision_id.clone(),
                left_revision_id: left.revision_id.clone(),
                right_revision_id: right.revision_id.clone(),
                author: "author".to_owned(),
                message: "conflict".to_owned(),
                expected_revision_id: right.revision_id.clone(),
            }))
            .await?
            .into_inner();
        assert!(conflict.revision.is_none());
        assert!(!conflict.conflicts.is_empty());

        let merged = service
            .merge_ontology(Request::new(proto::MergeOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                ontology_id: "people".to_owned(),
                base_revision_id: base.revision_id.clone(),
                left_revision_id: left.revision_id,
                right_revision_id: base.revision_id,
                author: "author".to_owned(),
                message: "merge".to_owned(),
                expected_revision_id: right.revision_id,
            }))
            .await?
            .into_inner()
            .revision
            .context("merged ontology revision missing")?;

        let publication = service
            .publish_ontology(Request::new(proto::PublishOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                ontology_id: "people".to_owned(),
                display_name: "People".to_owned(),
                publication_id: "publication-1".to_owned(),
                revision_id: merged.revision_id.clone(),
                metadata_json: br#"{"channel":"stable"}"#.to_vec(),
                create_only: false,
            }))
            .await?
            .into_inner();
        assert_eq!(publication.lifecycle, "production");
        let ontologies = service
            .list_ontologies(Request::new(proto::ListOntologiesRequest {
                tenant_id: "tenant_a".to_owned(),
                limit: 10,
            }))
            .await?
            .into_inner();
        assert_eq!(ontologies.ontologies.len(), 1);

        let created = service
            .put_pipeline(Request::new(proto::PutPipelineRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "daily".to_owned(),
                name: "Daily".to_owned(),
                owner_id: "owner".to_owned(),
                definition_json: pipeline(),
                author_id: "author".to_owned(),
                message: "create pipeline".to_owned(),
                expected_revision_id: None,
                create_only: true,
            }))
            .await?
            .into_inner();
        let binding = service
            .put_binding(Request::new(proto::PutBindingRequest {
                tenant_id: "tenant_a".to_owned(),
                binding: Some(proto::OntologyBinding {
                    pipeline_id: "daily".to_owned(),
                    block_id: "sink".to_owned(),
                    ontology_id: "people".to_owned(),
                    resource_ids: vec!["object.person".to_owned()],
                    resource_kinds: Vec::new(),
                    include_dependencies: true,
                    metadata_json: br#"{"purpose":"runtime"}"#.to_vec(),
                    updated_at_ms: 42,
                    head_revision_id: String::new(),
                }),
                expected_revision_id: created.head_revision_id,
            }))
            .await?
            .into_inner();
        let bindings = service
            .list_bindings(Request::new(proto::ListBindingsRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: Some("daily".to_owned()),
                limit: 10,
                revision_id: Some(binding.head_revision_id.clone()),
            }))
            .await?
            .into_inner();
        assert_eq!(bindings.bindings.len(), 1);

        let slice = service
            .get_ontology_slice(Request::new(proto::GetOntologySliceRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "daily".to_owned(),
                pipeline_revision_id: binding.head_revision_id.clone(),
                block_id: "sink".to_owned(),
            }))
            .await?
            .into_inner();
        assert_eq!(slice.ontology_id, "people");
        assert_eq!(slice.revision_id, merged.revision_id);

        let published = service
            .publish_pipeline(Request::new(proto::PublishPipelineRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "daily".to_owned(),
                revision_id: binding.head_revision_id.clone(),
            }))
            .await?
            .into_inner();
        assert_eq!(
            published.published_revision_id.as_deref(),
            Some(binding.head_revision_id.as_str())
        );
        let current_pipeline = service
            .get_pipeline(Request::new(proto::GetPipelineRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "daily".to_owned(),
                revision_id: None,
            }))
            .await?
            .into_inner();
        assert_eq!(
            current_pipeline.head_revision_id,
            published.head_revision_id
        );
        let pinned_pipeline = service
            .get_pipeline(Request::new(proto::GetPipelineRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "daily".to_owned(),
                revision_id: Some(binding.head_revision_id.clone()),
            }))
            .await?
            .into_inner();
        assert_eq!(pinned_pipeline.pipeline_id, "daily");
        let runtime = service
            .get_runtime_pipeline(Request::new(
                proto::GetRuntimePipelineRequest {
                    tenant_id: "tenant_a".to_owned(),
                    pipeline_id: "daily".to_owned(),
                    revision_id: binding.head_revision_id,
                },
            ))
            .await?
            .into_inner();
        assert_eq!(runtime.pipeline_id, "daily");
        assert_eq!(
            service
                .list_pipelines(Request::new(proto::ListPipelinesRequest {
                    tenant_id: "tenant_a".to_owned(),
                    limit: 10,
                }))
                .await?
                .into_inner()
                .pipelines
                .len(),
            1
        );
        assert_eq!(
            service
                .list_published_pipelines(Request::new(
                    proto::ListPublishedPipelinesRequest {
                        tenant_id: "tenant_a".to_owned(),
                        limit: 10,
                    },
                ))
                .await?
                .into_inner()
                .pipelines
                .len(),
            1
        );

        let deleted = service
            .delete_pipeline(Request::new(proto::DeletePipelineRequest {
                tenant_id: "tenant_a".to_owned(),
                pipeline_id: "daily".to_owned(),
                expected_revision_id: published.head_revision_id,
            }))
            .await?
            .into_inner();
        assert!(!deleted.revision_id.is_empty());
        let retired = service
            .retire_ontology(Request::new(proto::RetireOntologyRequest {
                tenant_id: "tenant_a".to_owned(),
                ontology_id: "people".to_owned(),
                publication_id: "publication-1".to_owned(),
                revision_id: merged.revision_id,
            }))
            .await?
            .into_inner();
        assert!(!retired.head_revision_id.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn boundary_helpers_reject_invalid_endpoints_and_json() {
        assert!(registry_client("not a URI").is_err());
        assert!(registry_client("http://registry:50051").is_ok());
        assert!(json_from_bytes(b"not-json").is_err());
        assert!(json_from_bytes(br#"{"valid":true}"#).is_ok());
        assert!(json_from_bytes(&vec![b' '; 16 * 1024 * 1024 + 1]).is_err());
    }

    #[test]
    fn status_mapping_does_not_expose_serialization_errors() {
        assert_eq!(
            domain_status(anyhow::anyhow!("registry revision changed")).code(),
            Code::Aborted
        );
        assert_eq!(
            domain_status(anyhow::anyhow!("ontology unavailable")).code(),
            Code::NotFound
        );
        assert_eq!(
            domain_status(anyhow::anyhow!("invalid tenant")).code(),
            Code::InvalidArgument
        );
        let storage = domain_status(anyhow::anyhow!(
            "S3 tenant marker read failed: secret endpoint detail"
        ));
        assert_eq!(storage.code(), Code::Unavailable);
        assert!(!storage.message().contains("secret"));
        assert_eq!(invalid_status("invalid").code(), Code::InvalidArgument);
        let internal = internal_status("secret serialization detail");
        assert_eq!(internal.code(), Code::Internal);
        assert!(!internal.message().contains("secret"));
        assert!(optional_json_bytes(None).is_ok_and(|value| value.is_empty()));
    }
}
