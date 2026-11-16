//! Typed gRPC boundary for Registry callers and the service binary.

use std::collections::BTreeSet;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use tonic::transport::{Channel, Endpoint};
use tonic::{Code, Request, Response, Status};

use crate::domain::{PipelineView, PutPipeline, Registry, TenantView};
use crate::gol::{GolDiagnostic, GolFile};
use crate::gol_service::{
    GolCompileApproval, GolDependency, GolSummary, GolWorkspace,
    SaveGolOntology,
};
use crate::proto;
use crate::state::GolBindingRecord;
use crate::storage::RevisionStore;

/// Cloneable generated client used by trusted internal services.
pub type RegistryClient = proto::registry_client::RegistryClient<
    tonic::service::interceptor::InterceptedService<
        Channel,
        GatewayCredential,
    >,
>;

/// Adds an operator-provisioned credential without trusting forwarded actor
/// headers.
#[derive(Clone, Default)]
pub struct GatewayCredential(
    Option<tonic::metadata::MetadataValue<tonic::metadata::Ascii>>,
);

impl tonic::service::Interceptor for GatewayCredential {
    fn call(
        &mut self,
        mut request: Request<()>,
    ) -> std::result::Result<Request<()>, Status> {
        if let Some(value) = &self.0 {
            request
                .metadata_mut()
                .insert("x-galadril-registry-key", value.clone());
        }
        Ok(request)
    }
}

/// Reads a fixed-size key without allocating for arbitrary local file
/// contents.
pub fn read_gateway_key(
    path: impl AsRef<std::path::Path>,
) -> Result<[u8; 32]> {
    use std::io::Read;
    let mut file = std::fs::File::open(path)
        .context("Registry Gateway credential unavailable")?;
    let mut key = [0u8; 32];
    file.read_exact(&mut key)
        .context("Registry Gateway credential must contain 32 bytes")?;
    let mut extra = [0u8; 1];
    ensure!(
        file.read(&mut extra)? == 0,
        "Registry Gateway credential must contain 32 bytes"
    );
    Ok(key)
}

fn encode_gateway_key(key: &[u8]) -> Result<String> {
    use std::fmt::Write;
    ensure!(
        key.len() == 32,
        "Registry Gateway credential must contain 32 bytes"
    );
    let mut value = String::with_capacity(64);
    for byte in key {
        write!(&mut value, "{byte:02x}")?;
    }
    Ok(value)
}

impl RegistryClient {
    /// Discovers initialized tenants without accepting event-supplied
    /// identities.
    pub async fn discover_tenants(&mut self) -> Result<Vec<String>> {
        let mut tenants = BTreeSet::new();
        let mut cursors = BTreeSet::new();
        let mut page_token = None;
        loop {
            let page = self
                .list_tenants(proto::ListTenantsRequest {
                    limit: 100,
                    page_token,
                })
                .await
                .context("Registry tenant discovery failed")?
                .into_inner();
            ensure!(
                page.tenant_ids.len() <= 100,
                "Registry tenant discovery page exceeds limit"
            );
            for tenant in page.tenant_ids {
                ensure!(
                    crate::storage::valid_tenant(&tenant),
                    "Registry returned invalid tenant identity"
                );
                tenants.insert(tenant);
            }
            let Some(next) = page.next_page_token else {
                return Ok(tenants.into_iter().collect());
            };
            crate::storage::validate_discovery_page(100, Some(&next))?;
            ensure!(
                cursors.insert(next.clone()),
                "Registry tenant discovery cursor repeated"
            );
            page_token = Some(next);
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
/// Internal endpoint and optional Gateway-only channel credential.
pub struct RegistryClientConfig {
    /// Registry gRPC endpoint; storage coordinates are intentionally absent.
    pub endpoint: String,
    /// Operator-owned Gateway credential; workload-only clients omit it.
    #[serde(default)]
    pub gateway_key_file: Option<std::path::PathBuf>,
}

impl RegistryClientConfig {
    /// Creates a typed Registry client from the validated endpoint.
    pub fn connect_lazy(&self) -> Result<RegistryClient> {
        let mut client = registry_client(&self.endpoint)?;
        let path = self.gateway_key_file.clone().or_else(|| {
            std::env::var_os("REGISTRY_GATEWAY_KEY_PATH")
                .map(std::path::PathBuf::from)
        });
        if let Some(path) = path {
            let key = read_gateway_key(path)?;
            client = registry_client_with_key(&self.endpoint, &key)?;
        }
        Ok(client)
    }
}

/// Creates a bounded lazy channel without issuing network I/O at startup.
pub fn registry_client(endpoint: &str) -> Result<RegistryClient> {
    let endpoint = Endpoint::from_shared(endpoint.to_owned())?
        .connect_timeout(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(30));
    Ok(proto::registry_client::RegistryClient::with_interceptor(
        endpoint.connect_lazy(),
        GatewayCredential::default(),
    ))
}

/// Creates a Gateway-authenticated client; pipeline workloads use
/// `registry_client`.
pub fn registry_client_with_key(
    endpoint: &str,
    key: &[u8],
) -> Result<RegistryClient> {
    let endpoint = Endpoint::from_shared(endpoint.to_owned())?
        .connect_timeout(std::time::Duration::from_secs(5))
        .timeout(std::time::Duration::from_secs(30));
    let value = encode_gateway_key(key)?.parse()?;
    Ok(proto::registry_client::RegistryClient::with_interceptor(
        endpoint.connect_lazy(),
        GatewayCredential(Some(value)),
    ))
}

/// gRPC implementation delegating every mutation to canonical domain
/// semantics.
pub struct RegistryGrpc<S> {
    registry: Arc<Registry<S>>,
    gateway_key: Option<[u8; 32]>,
}

impl<S> RegistryGrpc<S> {
    /// Shares one Registry domain instance across concurrent gRPC requests.
    pub fn new(registry: Arc<Registry<S>>) -> Self {
        Self {
            registry,
            gateway_key: None,
        }
    }

    /// Enables source RPCs only for the dedicated Gateway channel credential.
    pub fn with_gateway_key(mut self, key: &[u8]) -> Result<Self> {
        self.gateway_key =
            Some(Sha256::digest(encode_gateway_key(key)?.as_bytes()).into());
        Ok(self)
    }

    fn checked_gateway_request<T>(
        &self,
        request: Request<T>,
    ) -> Result<T, Status> {
        let denied =
            || Status::permission_denied("Gateway source channel required");
        let expected = self.gateway_key.as_ref().ok_or_else(denied)?;
        let mut values =
            request.metadata().get_all("x-galadril-registry-key").iter();
        let value = values
            .next()
            .and_then(|value| value.to_str().ok())
            .filter(|value| value.len() == 64)
            .ok_or_else(denied)?;
        if values.next().is_some() {
            return Err(denied());
        }
        let digest = Sha256::digest(value.as_bytes());
        if !bool::from(expected.as_slice().ct_eq(digest.as_slice())) {
            return Err(denied());
        }
        Ok(request.into_inner())
    }
}

#[tonic::async_trait]
impl<S> proto::registry_server::Registry for RegistryGrpc<S>
where
    S: RevisionStore + 'static,
{
    async fn list_tenants(
        &self,
        request: Request<proto::ListTenantsRequest>,
    ) -> Result<Response<proto::ListTenantsResponse>, Status> {
        let request = request.into_inner();
        let page = self
            .registry
            .list_tenants(
                if request.limit == 0 {
                    100
                } else {
                    request.limit
                },
                request.page_token.as_deref(),
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::ListTenantsResponse {
            tenant_ids: page.tenant_ids,
            next_page_token: page.next_page_token,
        }))
    }

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
        let input = PutPipeline {
            tenant_id: &request.tenant_id,
            pipeline_id: &request.pipeline_id,
            name: &request.name,
            owner_id: &request.owner_id,
            definition,
            author: &request.author_id,
            message: &request.message,
            expected_revision_id: request.expected_revision_id.as_deref(),
            create_only: request.create_only,
        };
        let view = if let Some(command) = request.authoring_command_json {
            let command = serde_json::from_slice(&command).map_err(|_| {
                Status::invalid_argument("invalid authoring command")
            })?;
            self.registry.author_pipeline(input, command).await
        } else {
            self.registry.put_pipeline(input).await
        }
        .map_err(domain_status)?;
        Ok(Response::new(pipeline_message(view)?))
    }

    async fn get_pipeline(
        &self,
        request: Request<proto::GetPipelineRequest>,
    ) -> Result<Response<proto::Pipeline>, Status> {
        let request = request.into_inner();
        let view = if let Some(revision) = request.revision_id.as_deref() {
            self.registry
                .pipeline_revision(
                    &request.tenant_id,
                    &request.pipeline_id,
                    revision,
                )
                .await
                .map_err(domain_status)?
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

    async fn save_gol_ontology(
        &self,
        request: Request<proto::SaveGolOntologyRequest>,
    ) -> Result<Response<proto::GolWorkspace>, Status> {
        let request = self.checked_gateway_request(request)?;
        let workspace = self
            .registry
            .save_gol_ontology(SaveGolOntology {
                tenant_id: &request.tenant_id,
                ontology_id: &request.ontology_id,
                display_name: &request.display_name,
                files: request
                    .files
                    .into_iter()
                    .map(|file| GolFile {
                        path: file.path,
                        text: file.text,
                    })
                    .collect(),
                author: &request.author,
                expected_revision: request.expected_revision.as_deref(),
            })
            .await
            .map_err(domain_status)?;
        Ok(Response::new(workspace_message(workspace)))
    }

    async fn get_gol_workspace(
        &self,
        request: Request<proto::GetGolWorkspaceRequest>,
    ) -> Result<Response<proto::GolWorkspace>, Status> {
        let request = self.checked_gateway_request(request)?;
        let workspace = self
            .registry
            .gol_workspace(
                &request.tenant_id,
                &request.ontology_id,
                request.source_revision.as_deref(),
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(workspace_message(workspace)))
    }

    async fn preview_gol_dependencies(
        &self,
        request: Request<proto::PreviewGolDependenciesRequest>,
    ) -> Result<Response<proto::GolDependencyPreview>, Status> {
        let request = self.checked_gateway_request(request)?;
        let preview = self
            .registry
            .preview_gol_dependencies(
                &request.tenant_id,
                &request.ontology_id,
                &request.source_revision,
                &request.approved_owners,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::GolDependencyPreview {
            required_owners: preview.required_owners,
            plan_pin: preview.plan_pin,
            dependencies: preview
                .dependencies
                .into_iter()
                .map(dependency_message)
                .collect(),
        }))
    }

    async fn validate_gol_ontology(
        &self,
        request: Request<proto::ValidateGolOntologyRequest>,
    ) -> Result<Response<proto::GolValidation>, Status> {
        let request = self.checked_gateway_request(request)?;
        let result = self
            .registry
            .validate_gol_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.source_revision,
                &GolCompileApproval {
                    plan_pin: request.plan_pin,
                    approved_owners: request.approved_owners,
                },
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::GolValidation {
            ontology_id: result.ontology_id,
            source_revision: result.source_revision,
            status: result.status,
            diagnostics: result
                .diagnostics
                .into_iter()
                .map(diagnostic_message)
                .collect(),
            dependencies: result
                .dependencies
                .into_iter()
                .map(dependency_message)
                .collect(),
        }))
    }

    async fn publish_gol_ontology(
        &self,
        request: Request<proto::PublishGolOntologyRequest>,
    ) -> Result<Response<proto::GolPublication>, Status> {
        let request = self.checked_gateway_request(request)?;
        let result = self
            .registry
            .publish_gol_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.source_revision,
                request.expected_generation.as_deref(),
                &request.author_id,
                &GolCompileApproval {
                    plan_pin: request.plan_pin,
                    approved_owners: request.approved_owners,
                },
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::GolPublication {
            ontology_id: result.ontology_id,
            source_revision: result.source_revision,
            generation: result.generation,
        }))
    }

    async fn delete_gol_ontology(
        &self,
        request: Request<proto::DeleteGolOntologyRequest>,
    ) -> Result<Response<proto::DeleteGolOntologyResponse>, Status> {
        let request = self.checked_gateway_request(request)?;
        let revision = self
            .registry
            .delete_gol_ontology(
                &request.tenant_id,
                &request.ontology_id,
                &request.expected_revision,
                &request.author,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::DeleteGolOntologyResponse { revision }))
    }

    async fn list_gol_ontologies(
        &self,
        request: Request<proto::ListGolOntologiesRequest>,
    ) -> Result<Response<proto::ListGolOntologiesResponse>, Status> {
        let request = self.checked_gateway_request(request)?;
        let result = self
            .registry
            .list_gol_ontologies(
                &request.tenant_id,
                if request.limit == 0 {
                    100
                } else {
                    request.limit as usize
                },
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::ListGolOntologiesResponse {
            ontologies: result.into_iter().map(summary_message).collect(),
        }))
    }

    async fn put_binding(
        &self,
        request: Request<proto::PutBindingRequest>,
    ) -> Result<Response<proto::GolBinding>, Status> {
        let request = self.checked_gateway_request(request)?;
        let binding = request.binding.ok_or_else(|| {
            Status::invalid_argument("GOL binding is required")
        })?;
        let input = GolBindingRecord {
            pipeline_id: binding.pipeline_id,
            block_id: binding.block_id,
            ontology_id: binding.ontology_id,
            generation: String::new(),
            source_revision: String::new(),
            symbols: binding.symbols,
            updated_at_ms: 0,
        };
        let (binding, revision) = self
            .registry
            .put_binding(
                &request.tenant_id,
                &request.expected_revision_id,
                input,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(binding_message(binding, &revision)))
    }

    async fn list_bindings(
        &self,
        request: Request<proto::ListBindingsRequest>,
    ) -> Result<Response<proto::ListBindingsResponse>, Status> {
        let request = self.checked_gateway_request(request)?;
        let bindings = self
            .registry
            .list_bindings(
                &request.tenant_id,
                request.pipeline_id.as_deref(),
                request.revision_id.as_deref(),
                if request.limit == 0 {
                    100
                } else {
                    request.limit as usize
                },
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(proto::ListBindingsResponse {
            bindings: bindings
                .into_iter()
                .map(|binding| {
                    binding_message(
                        binding,
                        request.revision_id.as_deref().unwrap_or_default(),
                    )
                })
                .collect(),
        }))
    }

    async fn get_gol_binding(
        &self,
        request: Request<proto::GetGolBindingRequest>,
    ) -> Result<Response<proto::GolBinding>, Status> {
        let request = self.checked_gateway_request(request)?;
        let binding = self
            .registry
            .gol_binding(
                &request.tenant_id,
                &request.pipeline_id,
                &request.pipeline_revision_id,
                &request.block_id,
            )
            .await
            .map_err(domain_status)?;
        Ok(Response::new(binding_message(
            binding,
            &request.pipeline_revision_id,
        )))
    }
}

fn summary_message(summary: GolSummary) -> proto::GolSummary {
    proto::GolSummary {
        ontology_id: summary.ontology_id,
        display_name: summary.display_name,
        source_revision: summary.source_revision,
        active_generation: summary.active_generation,
        active_source_revision: summary.active_source_revision,
        status: summary.status,
    }
}

fn diagnostic_message(item: GolDiagnostic) -> proto::GolDiagnostic {
    proto::GolDiagnostic {
        code: item.code,
        message: item.message,
        path: item.path,
        start: item.start as u64,
        end: item.end as u64,
        severity: item.severity,
    }
}

fn dependency_message(item: GolDependency) -> proto::GolDependency {
    proto::GolDependency {
        ontology_id: item.ontology_id,
        generation: item.generation,
        source_revision: item.source_revision,
    }
}

fn workspace_message(workspace: GolWorkspace) -> proto::GolWorkspace {
    proto::GolWorkspace {
        summary: Some(summary_message(workspace.summary)),
        files: workspace
            .files
            .into_iter()
            .map(|file| proto::GolFile {
                path: file.path,
                text: file.text,
            })
            .collect(),
        diagnostics: workspace
            .diagnostics
            .into_iter()
            .map(diagnostic_message)
            .collect(),
        dependencies: workspace
            .dependencies
            .into_iter()
            .map(dependency_message)
            .collect(),
    }
}

fn binding_message(
    binding: GolBindingRecord,
    revision: &str,
) -> proto::GolBinding {
    proto::GolBinding {
        pipeline_id: binding.pipeline_id,
        block_id: binding.block_id,
        ontology_id: binding.ontology_id,
        generation: binding.generation,
        source_revision: binding.source_revision,
        symbols: binding.symbols,
        updated_at_ms: binding.updated_at_ms,
        head_revision_id: revision.into(),
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
        authoring_json: serde_json::to_vec(&view.record.authoring)
            .map_err(|_| Status::internal("authoring serialization failed"))?,
        previous_revision_id: view.record.previous_revision_id,
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

fn domain_status(error: anyhow::Error) -> Status {
    if error.is::<crate::gol_service::PublicationCycle>() {
        return Status::failed_precondition(error.to_string());
    }
    if let Some(remote) =
        error.downcast_ref::<galadril_ontology::archive::remote::RemoteError>()
    {
        use galadril_ontology::archive::remote::RemoteError;
        return match remote {
            RemoteError::Conflict => {
                Status::aborted("Registry publication changed")
            },
            RemoteError::Integrity => {
                Status::data_loss("Registry persisted state failed validation")
            },
            RemoteError::Missing => {
                Status::not_found("Registry artifact unavailable")
            },
            RemoteError::Capacity => Status::resource_exhausted(
                "Registry publication capacity exceeded",
            ),
            RemoteError::Expired | RemoteError::Retained => {
                Status::failed_precondition(
                    "Registry generation is unavailable",
                )
            },
            RemoteError::Configuration | RemoteError::UnsupportedStorage => {
                Status::failed_precondition(
                    "Registry artifact storage is incompatible",
                )
            },
            _ => Status::unavailable("Registry storage is unavailable"),
        };
    }
    if error.is::<reqwest::Error>() || error.is::<tonic::Status>() {
        tracing::error!(event.name = "registry.backend_transport_failed");
        return Status::unavailable("Registry storage is unavailable");
    }
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
        message.contains("://") ||
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
#[path = "../tests/grpc_status.rs"]
mod status_tests;
