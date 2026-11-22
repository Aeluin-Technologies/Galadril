//! Retain trusted input and authorization provenance across worker requests.

mod business;

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use galadril_ontology::archive::remote::RemoteArchive;
use galadril_ontology::authorization::{
    Access, Decision, Flow, Instance, Operation, Route,
};
use galadril_ontology::export::{
    ExportOptions, ExportRequest, IntegerEncoding, SchemaPart,
};
use galadril_ontology::{Name, Reference};
use galadril_registry::proto::GetGolBindingRequest;
use sha2::{Digest, Sha256};
use tokio::sync::Mutex as AsyncMutex;
use uuid::Uuid;

use super::admission::{Admission, VerifiedInput};
use super::effect_scope::Permits;
use super::receipts::{Dependency, ReceiptScope};
use super::runtime::{
    BeginEffects, EffectRequest, EffectResponse, RuntimeError, RuntimeLease,
    RuntimeRequest, RuntimeSymbol,
};
use super::{GolService, owner, resolve, snapshot, symbol};
use crate::application::usecases::authorization::{Permission, QueryContext};

struct Lease {
    pipeline: String,
    revision: String,
    block: String,
    binding_pipeline: String,
    ontology: String,
    source_revision: String,
    archive: Arc<RemoteArchive>,
    targets: BTreeSet<String>,
}

struct Worker {
    admission: Arc<Admission>,
    gate: Arc<AsyncMutex<()>>,
    invalid: Arc<AtomicBool>,
    facts: QueryContext,
    leases: Mutex<BTreeMap<String, Arc<Lease>>>,
    dependencies: Mutex<BTreeSet<Dependency>>,
    pins: Mutex<BTreeMap<String, String>>,
    expires: Instant,
}

/// Selects the deployment floor for one verified tenant and upload group.
fn ingress_floor<'a>(
    floors: &'a BTreeMap<String, Flow>,
    tenant: &str,
    group: &str,
) -> Option<&'a Flow> {
    floors.get(&format!("{tenant}/{group}"))
}

/// Matches the private worker transport's IEEE-754 values and exact integers.
fn worker_json_options() -> ExportOptions {
    ExportOptions {
        integers: IntegerEncoding::DecimalStrings,
        allow_float_rounding: true,
        ..ExportOptions::default()
    }
}

#[cfg(test)]
mod tests {
    use galadril_ontology::Classification;

    use super::*;

    #[test]
    fn worker_json_representation_accepts_native_floats_and_exact_integers() {
        let options = worker_json_options();
        assert!(options.allow_float_rounding);
        assert_eq!(options.integers, IntegerEncoding::DecimalStrings);
        assert!(!ExportOptions::default().allow_float_rounding);
    }

    #[test]
    fn ingress_floor_requires_the_exact_tenant_and_group() {
        let public = Flow::new(Classification::Public);
        let restricted = Flow::new(Classification::Restricted);
        let floors = BTreeMap::from([
            ("tenant_a/default".into(), public.clone()),
            ("tenant_b/default".into(), restricted.clone()),
            ("default".into(), public.clone()),
        ]);

        assert_eq!(
            ingress_floor(&floors, "tenant_a", "default"),
            Some(&public)
        );
        assert_eq!(
            ingress_floor(&floors, "tenant_b", "default"),
            Some(&restricted)
        );
        assert!(ingress_floor(&floors, "tenant_c", "default").is_none());
        assert!(ingress_floor(&floors, "tenant_a", "other").is_none());
    }
}

pub(super) struct Workers {
    entries: Mutex<BTreeMap<String, Arc<Worker>>>,
    permits: Permits,
}

impl Workers {
    pub fn new() -> Result<Self> {
        Ok(Self {
            entries: Mutex::new(BTreeMap::new()),
            permits: Permits::new(1024, Duration::from_secs(30))?,
        })
    }
}

impl GolService {
    /// Only the authenticated dispatcher can mint an independent input domain.
    /// Deployment floors classify the channel; user clearance is merely a
    /// ceiling.
    pub async fn issue_ingress(
        &self,
        tenant: &str,
        user: &str,
        source_object_id: &str,
        revision: &str,
        group: &str,
        facts: &QueryContext,
    ) -> Result<String> {
        let backend = self.backend()?;
        let actor = self.actor(tenant, user).await?;
        let floor =
            ingress_floor(&backend.profile.ingress_floors, tenant, group)
                .context("GOL ingress channel is not approved")?;
        ensure!(
            source_object_id.starts_with(&format!("{tenant}/")),
            "GOL ingress scope unavailable"
        );
        let source_object_id = format!("raw:{source_object_id}");
        let now = Instant::now();
        let token = self.admissions.admit(
            actor,
            VerifiedInput {
                source_object_id: source_object_id.as_str().into(),
                revision: revision.into(),
                flow: floor.join(&backend.profile.context_floor),
            },
            now,
        )?;
        let admitted = self
            .admissions
            .resolve(&token, tenant, &source_object_id, now)?
            .context("GOL admission unavailable")?;
        let mut facts = facts.clone();
        // Interactive MFA is not a renewable background credential.
        facts.mfa_verified = false;
        let mut entries = self
            .workers
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?;
        entries.retain(|_, value| now < value.expires);
        ensure!(entries.len() < 1024, "GOL runtime capacity reached");
        entries.insert(
            token.clone(),
            Arc::new(Worker {
                admission: admitted,
                gate: Arc::new(AsyncMutex::new(())),
                invalid: Arc::new(AtomicBool::new(false)),
                facts,
                leases: Mutex::new(BTreeMap::new()),
                dependencies: Mutex::new(BTreeSet::from([Dependency {
                    ontology: String::new(),
                    target: String::new(),
                    instance_id: source_object_id,
                    revision: revision.to_owned(),
                    operation: Operation::Read,
                }])),
                pins: Mutex::new(BTreeMap::new()),
                expires: now
                    .checked_add(Duration::from_secs(900))
                    .context("GOL runtime unavailable")?,
            }),
        );
        Ok(token)
    }

    /// A dedicated deployment credential authenticates the private worker
    /// channel.
    pub(crate) fn verify_worker_key(&self, encoded: &str) -> bool {
        let Some(key) = self
            .backend
            .as_ref()
            .and_then(|backend| backend.worker_key.as_ref())
        else {
            return false;
        };
        if encoded.len() != 64 {
            return false;
        }
        let mut candidate = [0_u8; 32];
        for (slot, pair) in
            candidate.iter_mut().zip(encoded.as_bytes().chunks_exact(2))
        {
            let Some(high) =
                pair.first().and_then(|v| char::from(*v).to_digit(16))
            else {
                return false;
            };
            let Some(low) =
                pair.get(1).and_then(|v| char::from(*v).to_digit(16))
            else {
                return false;
            };
            *slot = ((high << 4) | low) as u8;
        }
        // Ring performs the comparison in constant time without exposing keys.
        let proposed = ring::hmac::sign(
            &ring::hmac::Key::new(ring::hmac::HMAC_SHA256, &candidate),
            b"galadril.gol.worker.v1",
        );
        ring::hmac::verify(key, b"galadril.gol.worker.v1", proposed.as_ref())
            .is_ok()
    }

    pub(crate) async fn runtime_open(
        &self,
        request: RuntimeRequest,
    ) -> Result<RuntimeLease, RuntimeError> {
        if request.tenant_id.len() > 128 ||
            request.pipeline_id.len() > 128 ||
            request.pipeline_revision_id.len() > 128 ||
            request.block_id.len() > 128 ||
            request.source_object_id.len() > 1024 ||
            request.admission.len() != 32
        {
            return Err(RuntimeError::Invalid);
        }
        self.open_worker(request).await.map_err(|error| {
            tracing::warn!(event.name = "gol.worker.open.denied");
            drop(error);
            RuntimeError::Denied
        })
    }

    async fn open_worker(
        &self,
        request: RuntimeRequest,
    ) -> Result<RuntimeLease> {
        let worker = self.worker(
            &request.admission,
            &request.tenant_id,
            &request.source_object_id,
        )?;
        let _boundary = self.worker_boundary(&worker).await?;
        self.verify_worker(&worker, &request.admission, &request.pipeline_id)
            .await?;
        let binding = self
            .registry
            .clone()
            .get_gol_binding(GetGolBindingRequest {
                tenant_id: request.tenant_id.clone(),
                pipeline_id: request.pipeline_id.clone(),
                pipeline_revision_id: request.pipeline_revision_id.clone(),
                block_id: request.block_id.clone(),
            })
            .await
            .context("GOL binding unavailable")?
            .into_inner();
        if binding.pipeline_id != request.pipeline_id {
            self.verify_pipeline_execute(&worker, &binding.pipeline_id)
                .await?;
        }
        let existing = {
            worker
                .leases
                .lock()
                .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
                .iter()
                .find(|(_, lease)| {
                    lease.pipeline == request.pipeline_id &&
                        lease.revision == request.pipeline_revision_id &&
                        lease.block == request.block_id
                })
                .map(|(id, lease)| (id.clone(), Arc::clone(lease)))
        };
        let (context_id, lease) = if let Some((id, lease)) = existing {
            ensure!(
                lease.ontology == binding.ontology_id &&
                    lease.binding_pipeline == binding.pipeline_id &&
                    lease.archive.generation().to_string() ==
                        binding.generation &&
                    lease.source_revision == binding.source_revision,
                "GOL binding changed"
            );
            (id, lease)
        } else {
            let archive = self
                .backend()?
                .open(
                    &request.tenant_id,
                    &binding.ontology_id,
                    Some(&binding.generation),
                )
                .await?;
            let provenance =
                archive.provenance().context("GOL provenance unavailable")?;
            ensure!(
                provenance.source_revision.as_ref() == binding.source_revision,
                "GOL source binding changed"
            );
            {
                let mut pins = worker
                    .pins
                    .lock()
                    .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?;
                for (name, generation) in std::iter::once((
                    binding.ontology_id.clone(),
                    binding.generation.clone(),
                ))
                .chain(provenance.dependencies.iter().map(
                    |(name, generation)| {
                        (name.to_string(), generation.to_string())
                    },
                )) {
                    ensure!(
                        pins.get(&name)
                            .is_none_or(|existing| existing == &generation),
                        "GOL computation mixes generations"
                    );
                    pins.insert(name, generation);
                }
            }
            let lease = Arc::new(Lease {
                pipeline: request.pipeline_id,
                revision: request.pipeline_revision_id,
                block: request.block_id,
                binding_pipeline: binding.pipeline_id,
                ontology: binding.ontology_id,
                source_revision: binding.source_revision,
                targets: binding.symbols.into_iter().collect(),
                archive: Arc::new(archive),
            });
            ensure!(
                !lease.targets.is_empty() && lease.targets.len() <= 1024,
                "GOL binding selection unavailable"
            );
            let id = Uuid::new_v4().simple().to_string();
            let mut leases = worker
                .leases
                .lock()
                .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?;
            ensure!(leases.len() < 256, "GOL runtime capacity reached");
            leases.insert(id.clone(), Arc::clone(&lease));
            (id, lease)
        };
        self.recheck_dependencies(&worker).await?;
        let backend = self.backend()?;
        ensure!(backend.signer.is_some(), "GOL effect signing unavailable");
        let actor = self
            .actor(&request.tenant_id, &worker.admission.actor.subject_id)
            .await?;
        let mut symbols = Vec::new();
        let mut schemas = BTreeMap::new();
        for key in &lease.targets {
            let target = resolve(&lease.archive, key)?;
            let access =
                backend.metadata_access(&request.tenant_id, target.clone())?;
            let prepared = snapshot::Snapshot::prepare(
                &lease.archive,
                &access,
                &actor,
                Arc::clone(&worker.admission.context),
                &worker.facts,
                &backend.authority,
                &backend.profile.context_floor,
            )
            .await?;
            let projection = prepared.projection(access, BTreeSet::new())?;
            let Some(description) = lease
                .archive
                .describe(&projection, &prepared, &backend.provider)
                .await
            else {
                continue;
            };
            let mut fields = BTreeSet::new();
            let properties = description.properties.iter().map(|property| {
                fields.insert(property.name.clone());
                let ty = match &property.kind {
                    galadril_ontology::FieldKind::Key(ty) | galadril_ontology::FieldKind::Value(ty) => ty.clone(),
                    galadril_ontology::FieldKind::OwnedAttribute { attribute, .. } => galadril_ontology::TypeRef::Named(attribute.clone()),
                    _ => anyhow::bail!("GOL property unavailable"),
                };
                Ok((property.name.to_string(), serde_json::json!({"type":super::type_name(&ty, &mut BTreeSet::new())?})))
            }).collect::<Result<BTreeMap<_,_>>>()?;
            let roles = description
                .roles
                .iter()
                .map(|role| {
                    (
                        role.name.to_string(),
                        galadril_registry::gol_service::symbol_key(
                            &role.role.entity,
                        ),
                    )
                })
                .collect();
            let version = owner(&target)?.version().to_string();
            let mut described = symbol(description)?;
            if matches!(target, Reference::Field(_)) {
                described.kind = "field".into();
            }
            let projection = prepared.projection(
                backend.metadata_access(&request.tenant_id, target.clone())?,
                fields.clone(),
            )?;
            let options = worker_json_options();
            let mappings = galadril_registry::gol::registered_json_mappings()?;
            if let Ok(document) = lease
                .archive
                .export_schema(
                    &ExportRequest {
                        projection: &projection,
                        part: SchemaPart::Value,
                        options: &options,
                        mappings: &mappings,
                    },
                    &prepared,
                    &backend.provider,
                )
                .await
            {
                schemas.insert(key.clone(), document.schema().clone());
            }
            let mut history = worker
                .dependencies
                .lock()
                .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?;
            history.insert(Dependency {
                ontology: lease.ontology.clone(),
                target: key.clone(),
                instance_id: "__schema".into(),
                revision: lease.source_revision.clone(),
                operation: Operation::Describe,
            });
            for field in fields {
                history.insert(Dependency {
                    ontology: lease.ontology.clone(),
                    target: format!("{key}.{field}"),
                    instance_id: "__schema".into(),
                    revision: lease.source_revision.clone(),
                    operation: Operation::Describe,
                });
            }
            ensure!(history.len() <= 1024, "GOL provenance budget exceeded");
            symbols.push(RuntimeSymbol {
                key: described.key,
                name: described.name,
                kind: described.kind,
                version,
                references: described.references,
                properties,
                roles,
            });
        }
        let version = lease
            .archive
            .versions()
            .ontologies
            .iter()
            .find(|module| module.ontology().to_string() == lease.ontology)
            .context("GOL module unavailable")?
            .version()
            .to_string();
        Ok(RuntimeLease {
            tenant_id: request.tenant_id,
            ontology_id: lease.ontology.clone(),
            ontology_version: version,
            source_revision: lease.source_revision.clone(),
            generation_id: lease.archive.generation().to_string(),
            context_id,
            symbols,
            schemas,
        })
    }

    fn worker(
        &self,
        token: &str,
        tenant: &str,
        source: &str,
    ) -> Result<Arc<Worker>> {
        self.workers.permits.reap(Instant::now())?;
        let admitted = self
            .admissions
            .resolve(token, tenant, source, Instant::now())?
            .context("GOL admission unavailable")?;
        let worker = self
            .workers
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
            .get(token)
            .cloned()
            .context("GOL admission unavailable")?;
        ensure!(
            Arc::ptr_eq(&admitted, &worker.admission) &&
                !worker.invalid.load(Ordering::Acquire) &&
                Instant::now() < worker.expires,
            "GOL admission unavailable"
        );
        Ok(worker)
    }

    async fn verify_worker(
        &self,
        worker: &Worker,
        token: &str,
        pipeline: &str,
    ) -> Result<()> {
        ensure!(
            !worker.invalid.load(Ordering::Acquire) &&
                Instant::now() < worker.expires,
            "GOL admission unavailable"
        );
        let tenant = worker.admission.actor.tenant.as_ref();
        let user = worker.admission.actor.subject_id.as_ref();
        self.identity.verify_user(tenant, user).await?;
        let source = worker
            .admission
            .input
            .source_object_id
            .strip_prefix("raw:")
            .context("GOL input unavailable")?;
        let backend = self.backend()?;
        let head = backend
            .client
            .head_object()
            .bucket(&backend.raw_bucket)
            .key(source)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("GOL input unavailable"))?;
        let metadata = head.metadata().context("GOL input unavailable")?;
        ensure!(
            head.e_tag() == Some(worker.admission.input.revision.as_ref()) &&
                metadata.get("tenant").map(String::as_str) == Some(tenant) &&
                metadata.get("owner").map(String::as_str) == Some(user) &&
                metadata.get("authz-origin").map(String::as_str) ==
                    Some("gateway") &&
                metadata.get("authz-gol-admission").map(String::as_str) ==
                    Some(token),
            "GOL input changed"
        );
        ensure!(
            self.auth
                .is_authorized(
                    user,
                    tenant,
                    Permission::View,
                    "raw",
                    source,
                    Some(&worker.facts)
                )
                .await?,
            "GOL input unavailable"
        );
        self.verify_pipeline_execute(worker, pipeline).await
    }

    async fn verify_pipeline_execute(
        &self,
        worker: &Worker,
        pipeline: &str,
    ) -> Result<()> {
        let actor = &worker.admission.actor;
        ensure!(
            self.auth
                .is_authorized(
                    &actor.subject_id,
                    &actor.tenant,
                    Permission::Execute,
                    "pipeline",
                    pipeline,
                    Some(&worker.facts)
                )
                .await?,
            "GOL pipeline unavailable"
        );
        Ok(())
    }

    async fn recheck_dependencies(&self, worker: &Worker) -> Result<()> {
        let dependencies = worker
            .dependencies
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
            .clone();
        for dependency in dependencies {
            self.recheck_dependency(worker, &dependency).await?;
        }
        Ok(())
    }

    async fn recheck_dependency(
        &self,
        worker: &Worker,
        dependency: &Dependency,
    ) -> Result<()> {
        let backend = self.backend()?;
        let actor = self
            .actor(
                &worker.admission.actor.tenant,
                &worker.admission.actor.subject_id,
            )
            .await?;
        if dependency.ontology.is_empty() && dependency.target.is_empty() {
            let key = dependency
                .instance_id
                .strip_prefix("raw:")
                .context("GOL source provenance unavailable")?;
            ensure!(
                key.starts_with(&format!("{}/", actor.tenant)),
                "GOL source provenance unavailable"
            );
            let head = backend
                .client
                .head_object()
                .bucket(&backend.raw_bucket)
                .key(key)
                .send()
                .await
                .map_err(|_| {
                    anyhow::anyhow!("GOL source provenance unavailable")
                })?;
            ensure!(
                head.e_tag() == Some(dependency.revision.as_str()) &&
                    self.auth
                        .is_authorized(
                            &actor.subject_id,
                            &actor.tenant,
                            Permission::View,
                            "raw",
                            key,
                            Some(&worker.facts)
                        )
                        .await?,
                "GOL source provenance unavailable"
            );
            return Ok(());
        }
        let archive = backend
            .open(&actor.tenant, &dependency.ontology, None)
            .await?;
        let target = resolve(&archive, &dependency.target)?;
        let access = Access {
            route: Route::new(target.clone(), dependency.operation),
            instance: Instance {
                tenant: actor.tenant.clone(),
                owner: owner(&target)?.clone(),
                id: dependency.instance_id.clone().into(),
            },
            destination: backend.profile.metadata_destination.clone(),
            purpose: backend.profile.metadata_purpose.clone(),
        };
        let prepared = if dependency.instance_id == "__schema" {
            snapshot::Snapshot::prepare(
                &archive,
                &access,
                &actor,
                Arc::clone(&worker.admission.context),
                &worker.facts,
                &backend.authority,
                &backend.profile.context_floor,
            )
            .await?
        } else {
            snapshot::Snapshot::prepare_data(
                &archive,
                &access,
                &actor,
                Arc::clone(&worker.admission.context),
                &worker.facts,
                &backend.authority,
                &backend.profile.context_floor,
                &dependency.revision,
                None,
            )
            .await?
        };
        ensure!(
            archive
                .authorize(&access, &prepared, &backend.provider)
                .await ==
                Decision::Allow,
            "GOL dependency unavailable"
        );
        Ok(())
    }

    pub(crate) async fn runtime_effect(
        &self,
        request: EffectRequest,
    ) -> Result<EffectResponse, RuntimeError> {
        match request {
            EffectRequest::Complete(request) => {
                let worker = self
                    .worker(
                        &request.admission,
                        &request.tenant_id,
                        &request.source_object_id,
                    )
                    .map_err(|_| RuntimeError::Denied)?;
                let lease = worker
                    .leases
                    .lock()
                    .map_err(|_| RuntimeError::Unavailable)?
                    .get(&request.context_id)
                    .cloned()
                    .ok_or(RuntimeError::Denied)?;
                if lease.pipeline != request.pipeline_id ||
                    lease.revision != request.pipeline_revision_id ||
                    lease.block != request.block_id ||
                    lease.archive.generation().to_string() !=
                        request.generation_id
                {
                    return Err(RuntimeError::Denied);
                }
                if !self
                    .workers
                    .permits
                    .complete(
                        &request.completion,
                        &request.tenant_id,
                        &request.context_id,
                        request.succeeded,
                        Instant::now(),
                    )
                    .map_err(|_| RuntimeError::Unavailable)?
                {
                    return Err(RuntimeError::Denied);
                }
                Ok(EffectResponse {
                    allowed: true,
                    permit: None,
                    receipts: Vec::new(),
                })
            },
            EffectRequest::Begin(request) => self
                .begin_effects(request)
                .await
                .map_err(|_| RuntimeError::Denied),
        }
    }

    async fn begin_effects(
        &self,
        request: BeginEffects,
    ) -> Result<EffectResponse> {
        ensure!(
            !request.effects.is_empty() && request.effects.len() <= 64,
            "GOL effect budget exceeded"
        );
        let worker = self.worker(
            &request.admission,
            &request.tenant_id,
            &request.source_object_id,
        )?;
        let boundary = self.worker_boundary(&worker).await?;
        self.verify_worker(&worker, &request.admission, &request.pipeline_id)
            .await?;
        let lease = worker
            .leases
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
            .get(&request.context_id)
            .cloned()
            .context("GOL lease unavailable")?;
        ensure!(
            lease.pipeline == request.pipeline_id &&
                lease.revision == request.pipeline_revision_id &&
                lease.block == request.block_id &&
                lease.archive.generation().to_string() ==
                    request.generation_id,
            "GOL lease unavailable"
        );
        if lease.binding_pipeline != lease.pipeline {
            self.verify_pipeline_execute(&worker, &lease.binding_pipeline)
                .await?;
        }
        self.recheck_dependencies(&worker).await?;
        let backend = self.backend()?;
        let actor = self
            .actor(&request.tenant_id, &worker.admission.actor.subject_id)
            .await?;
        let current = backend
            .open(&request.tenant_id, &lease.ontology, None)
            .await?;
        let mut checked = Vec::with_capacity(request.effects.len());
        let mut roots = Vec::with_capacity(request.effects.len());
        let mut outcome = worker.admission.context.current_label()?;
        for effect in &request.effects {
            ensure!(
                lease.targets.contains(&effect.target),
                "GOL target not bound"
            );
            let operation = match effect.operation.as_str() {
                "read" => Operation::Read,
                "write" => Operation::Write,
                "transfer" => Operation::Transfer,
                _ => anyhow::bail!("GOL effect not implemented by host"),
            };
            let generated_id;
            let id = match effect.instance_id.as_deref() {
                Some(id) => id,
                None if operation == Operation::Transfer => {
                    let local = serde_json::to_vec(&(
                        &effect.target,
                        &effect.destination,
                        &effect.purpose,
                    ))?;
                    generated_id = format!(
                        "gol:{:x}:{:x}",
                        Sha256::digest(
                            worker.admission.input.source_object_id.as_bytes()
                        ),
                        Sha256::digest(&local)
                    );
                    generated_id.as_str()
                },
                None => anyhow::bail!("GOL concrete instance is required"),
            };
            ensure!(
                !id.is_empty() && id != "__schema" && id.len() <= 256,
                "GOL instance unavailable"
            );
            let target = resolve(&lease.archive, &effect.target)?;
            let access = Access {
                route: Route::new(target.clone(), operation),
                instance: Instance {
                    tenant: actor.tenant.clone(),
                    owner: owner(&target)?.clone(),
                    id: id.into(),
                },
                destination: effect.destination.parse()?,
                purpose: effect.purpose.parse()?,
            };
            let receipt = match (&effect.receipt, &effect.proposal) {
                (Some(receipt), Some(value)) => Some(
                    backend
                        .signer
                        .as_ref()
                        .context("GOL provenance unavailable")?
                        .verify(receipt, &request.tenant_id, id, value)?,
                ),
                (None, _)
                    if operation == Operation::Write ||
                        (operation == Operation::Transfer &&
                            effect.instance_id.is_none()) =>
                {
                    None
                },
                _ => anyhow::bail!("GOL value provenance required"),
            };
            if let Some(receipt) = &receipt {
                ensure!(
                    receipt.target == target,
                    "GOL provenance target mismatch"
                );
                worker.admission.context.include_input(&receipt.flow)?;
                for dependency in &receipt.dependencies {
                    self.recheck_dependency(&worker, dependency).await?;
                }
                worker
                    .dependencies
                    .lock()
                    .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
                    .extend(receipt.dependencies.iter().cloned());
            }
            if operation == Operation::Transfer && effect.instance_id.is_none()
            {
                backend
                    .provider
                    .ensure_source_instance(
                        &access.instance,
                        &worker.admission.input.source_object_id,
                        &actor.subject_id,
                        &lease.ontology,
                    )
                    .await?;
            }
            // Opaque cross-calculation native state cannot be reclassified by
            // a request.
            if matches!(
                effect.destination.as_str(),
                "identity_state" | "causal_state"
            ) {
                worker
                    .admission
                    .context
                    .include_input(&backend.profile.shared_state_floor)?;
                anyhow::bail!("GOL shared-state provenance unavailable");
            }
            let mut prepared = snapshot::Snapshot::prepare_data(
                &lease.archive,
                &access,
                &actor,
                Arc::clone(&worker.admission.context),
                &worker.facts,
                &backend.authority,
                &backend.profile.context_floor,
                &worker.admission.input.revision,
                receipt.as_ref().map(|receipt| &receipt.flow),
            )
            .await?;
            let requirements =
                lease.archive.requirements(&access.route).await?;
            for descriptor in requirements.values() {
                outcome = outcome
                    .join(descriptor.control_flow())
                    .join(descriptor.flow());
            }
            let current_target = resolve(&current, &effect.target)?;
            for descriptor in current
                .requirements(&Route::new(current_target, operation))
                .await?
                .values()
            {
                outcome = outcome
                    .join(descriptor.control_flow())
                    .join(descriptor.flow());
            }
            if operation == Operation::Write {
                let proposal = effect
                    .proposal
                    .as_ref()
                    .context("GOL proposal required")?;
                ensure!(
                    matches!(target, Reference::Symbol(_)),
                    "GOL partial replacement needs authoritative state"
                );
                backend
                    .provider
                    .ensure_source_instance(
                        &access.instance,
                        &worker.admission.input.source_object_id,
                        &actor.subject_id,
                        &lease.ontology,
                    )
                    .await?;
                let contract = lease
                    .archive
                    .invariant_contract(&access, &prepared, &backend.provider)
                    .await
                    .context("GOL invariant contract unavailable")?;
                let fields = proposal
                    .as_object()
                    .context("GOL record proposal required")?
                    .keys()
                    .map(|name| name.parse())
                    .collect::<Result<BTreeSet<Name>, _>>()?;
                prepared
                    .proposed_fields(&lease.archive, &fields, owner(&target)?)
                    .await?;
                let metadata_access = backend
                    .metadata_access(&request.tenant_id, target.clone())?;
                let metadata = snapshot::Snapshot::prepare(
                    &lease.archive,
                    &metadata_access,
                    &actor,
                    Arc::clone(&worker.admission.context),
                    &worker.facts,
                    &backend.authority,
                    &backend.profile.context_floor,
                )
                .await?;
                let projection =
                    metadata.projection(metadata_access, fields.clone())?;
                let pair = snapshot::Pair {
                    metadata,
                    data: prepared,
                };
                let options = worker_json_options();
                let mappings =
                    galadril_registry::gol::registered_json_mappings()?;
                lease
                    .archive
                    .validate_proposal_for(
                        &ExportRequest {
                            projection: &projection,
                            part: SchemaPart::Value,
                            options: &options,
                            mappings: &mappings,
                        },
                        proposal,
                        &access,
                        &pair,
                        &backend.provider,
                        &business::Business::new(contract, proposal),
                    )
                    .await?;
                for field in fields {
                    let mut field_access =
                        access_clone(&access, Operation::Write);
                    field_access.route.target =
                        galadril_ontology::FieldRef::new(
                            owner(&target)?.clone(),
                            field,
                        )
                        .into();
                    for descriptor in lease
                        .archive
                        .requirements(&field_access.route)
                        .await?
                        .values()
                    {
                        outcome = outcome
                            .join(descriptor.flow())
                            .join(descriptor.control_flow());
                    }
                    let active_field = resolve(
                        &current,
                        &reference_key(&field_access.route.target),
                    )?;
                    for descriptor in current
                        .requirements(&Route::new(
                            active_field,
                            Operation::Write,
                        ))
                        .await?
                        .values()
                    {
                        outcome = outcome
                            .join(descriptor.flow())
                            .join(descriptor.control_flow());
                    }
                    checked.push(field_access);
                }
            }
            roots.push(access_clone(&access, operation));
            checked.push(access);
        }
        // Workers observe commit/error status directly. Require its read route
        // as well as its label; a blind write grant cannot authorize that
        // result.
        let observations = checked
            .iter()
            .filter(|access| access.route.operation == Operation::Write)
            .map(|access| access_clone(access, Operation::Read))
            .collect::<Vec<_>>();
        for access in &observations {
            for descriptor in
                lease.archive.requirements(&access.route).await?.values()
            {
                outcome = outcome
                    .join(descriptor.flow())
                    .join(descriptor.control_flow());
            }
            let target =
                resolve(&current, &reference_key(&access.route.target))?;
            for descriptor in current
                .requirements(&Route::new(target, Operation::Read))
                .await?
                .values()
            {
                outcome = outcome
                    .join(descriptor.flow())
                    .join(descriptor.control_flow());
            }
        }
        checked.extend(observations);
        // Account for all results and control inputs before testing any
        // destination.
        worker.admission.context.include_input(&outcome)?;
        for access in &checked {
            let prepared = snapshot::Snapshot::prepare_data(
                &lease.archive,
                access,
                &actor,
                Arc::clone(&worker.admission.context),
                &worker.facts,
                &backend.authority,
                &backend.profile.context_floor,
                &worker.admission.input.revision,
                None,
            )
            .await?;
            ensure!(
                lease
                    .archive
                    .authorize(access, &prepared, &backend.provider)
                    .await ==
                    Decision::Allow,
                "GOL effect denied"
            );
            // A newly published policy must also approve an already pinned
            // type reader.
            let current_target =
                resolve(&current, &reference_key(&access.route.target))?;
            let mut current_access =
                access_clone(access, access.route.operation);
            current_access.route.target = current_target.clone();
            current_access.instance.owner = owner(&current_target)?.clone();
            let current_snapshot = snapshot::Snapshot::prepare_data(
                &current,
                &current_access,
                &actor,
                Arc::clone(&worker.admission.context),
                &worker.facts,
                &backend.authority,
                &backend.profile.context_floor,
                &worker.admission.input.revision,
                None,
            )
            .await?;
            ensure!(
                current
                    .authorize(
                        &current_access,
                        &current_snapshot,
                        &backend.provider
                    )
                    .await ==
                    Decision::Allow,
                "GOL active policy denied"
            );
        }
        let flow = worker.admission.context.current_label()?;
        // Persist the original prerequisite routes, even if a future contract
        // removes them. A cached derived value still depends on its old
        // inputs.
        let mut observed = Vec::new();
        for access in &checked {
            if access.route.operation == Operation::Read {
                for route in
                    lease.archive.requirements(&access.route).await?.keys()
                {
                    if route.operation == Operation::Read {
                        observed.push(Dependency {
                            ontology: lease.ontology.clone(),
                            target: reference_key(&route.target),
                            instance_id: access.instance.id.to_string(),
                            revision: worker
                                .admission
                                .input
                                .revision
                                .to_string(),
                            operation: Operation::Read,
                        });
                    }
                }
            }
        }
        let mut dependencies = worker
            .dependencies
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?;
        dependencies.extend(observed);
        ensure!(dependencies.len() <= 1024, "GOL provenance budget exceeded");
        let receipts = request
            .effects
            .iter()
            .zip(&roots)
            .map(|(effect, access)| {
                if access.route.operation != Operation::Write {
                    return Ok(None);
                }
                backend
                    .signer
                    .as_ref()
                    .context("GOL signing unavailable")?
                    .issue(
                        &ReceiptScope {
                            tenant: &request.tenant_id,
                            target: &access.route.target,
                            instance_id: &access.instance.id,
                            generation: &request.generation_id,
                            source_revision: &lease.source_revision,
                            dependencies: &dependencies,
                        },
                        &flow,
                        effect
                            .proposal
                            .as_ref()
                            .context("GOL proposal required")?,
                    )
                    .map(Some)
            })
            .collect::<Result<Vec<_>>>()?;
        drop(dependencies);
        let permit = self.workers.permits.begin(
            &request.tenant_id,
            &request.context_id,
            Arc::clone(&worker.invalid),
            boundary,
            Instant::now(),
        )?;
        Ok(EffectResponse {
            allowed: true,
            permit: Some(permit),
            receipts,
        })
    }

    async fn worker_boundary(
        &self,
        worker: &Worker,
    ) -> Result<tokio::sync::OwnedMutexGuard<()>> {
        match tokio::time::timeout(
            Duration::from_secs(31),
            Arc::clone(&worker.gate).lock_owned(),
        )
        .await
        {
            Ok(boundary) => Ok(boundary),
            Err(_) => {
                worker.invalid.store(true, Ordering::Release);
                self.workers.permits.reap(Instant::now())?;
                anyhow::bail!("GOL calculation outcome unavailable")
            },
        }
    }
}

fn access_clone(access: &Access, operation: Operation) -> Access {
    Access {
        route: Route::new(access.route.target.clone(), operation),
        instance: access.instance.clone(),
        destination: access.destination.clone(),
        purpose: access.purpose.clone(),
    }
}

pub(super) fn reference_key(reference: &Reference) -> String {
    match reference {
        Reference::Symbol(symbol) => {
            galadril_registry::gol_service::symbol_key(symbol)
        },
        Reference::Field(field) => format!(
            "{}.{}",
            galadril_registry::gol_service::symbol_key(&field.owner),
            field.name
        ),
        _ => String::new(),
    }
}
