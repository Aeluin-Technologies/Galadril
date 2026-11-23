//! Retain trusted input and authorization provenance across worker requests.

mod business;

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use chrono::Timelike as _;
use galadril_ontology::archive::remote::RemoteArchive;
use galadril_ontology::authorization::{
    Access, Decision, ExecutionContext, Flow, Instance, Operation, Route,
};
use galadril_ontology::export::{
    ExportOptions, ExportRequest, IntegerEncoding, SchemaPart,
};
use galadril_ontology::{Name, Reference};
use galadril_registry::proto::GetGolBindingRequest;
use sha2::{Digest, Sha256};
use tokio::sync::Mutex as AsyncMutex;
use uuid::Uuid;

use super::admission::{Actor, Admission, VerifiedInput};
use super::effect_scope::Permits;
use super::receipts::{Dependency, LineageOrigin, ReceiptScope};
use super::runtime::{
    BeginEffects, EffectRequest, EffectResponse, ResumeRequest,
    ResumeResponse, RuntimeError, RuntimeLease, RuntimeRequest, RuntimeSymbol,
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
    source_marker: [u8; 32],
    resume: Option<ResumedScope>,
    expires: Instant,
}

struct ResumedScope {
    key: [u8; 32],
    pipeline: String,
    revision: String,
    block: String,
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
                source_marker: Sha256::digest(token.as_bytes()).into(),
                resume: None,
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
        let mut phase = "admission";
        self.open_worker(request, &mut phase)
            .await
            .map_err(|error| {
                tracing::warn!(event.name = "gol.worker.open.denied", phase);
                drop(error);
                RuntimeError::Denied
            })
    }

    pub(crate) async fn runtime_resume(
        &self,
        request: ResumeRequest,
    ) -> Result<ResumeResponse, RuntimeError> {
        let mut phase = "lineage";
        self.resume_worker(request, &mut phase)
            .await
            .map(|admission| ResumeResponse { admission })
            .map_err(|_| {
                tracing::warn!(event.name = "gol.worker.resume.denied", phase);
                RuntimeError::Denied
            })
    }

    async fn resume_worker(
        &self,
        request: ResumeRequest,
        phase: &mut &'static str,
    ) -> Result<String> {
        ensure!(
            !request.block_id.is_empty() && request.block_id.len() <= 128,
            "GOL lineage scope rejected"
        );
        let backend = self.backend()?;
        let receipt = backend
            .signer
            .as_ref()
            .context("GOL provenance unavailable")?
            .verify_lineage(
                &request.lineage,
                &request.tenant_id,
                &request.source_object_id,
                &request.pipeline_id,
                &request.pipeline_revision_id,
            )?;
        receipt.verify_command(&request.block_id, request.command_id)?;
        let origin = receipt.lineage.context("GOL lineage unavailable")?;
        *phase = "principal_and_input";
        let actor = self.actor(&request.tenant_id, &origin.subject_id).await?;
        ensure!(actor.issuer.as_ref() == origin.issuer, "GOL issuer changed");
        let input = VerifiedInput {
            source_object_id: origin.source_object_id.into(),
            revision: origin.source_revision.into(),
            flow: receipt.flow.join(&backend.profile.context_floor),
        };
        let facts = QueryContext {
            hour_utc: i64::from(chrono::Utc::now().hour()),
            ..Default::default()
        };
        self.verify_worker_source(
            &actor,
            &input,
            &origin.source_marker,
            &facts,
        )
        .await?;
        self.verify_actor_execute(&actor, &facts, &request.pipeline_id)
            .await?;

        *phase = "admission";
        let mut key = Sha256::new();
        key.update(request.command_id.as_bytes());
        key.update(Sha256::digest(request.lineage.as_bytes()));
        key.update(request.block_id.as_bytes());
        let key: [u8; 32] = key.finalize().into();
        let now = Instant::now();
        let mut entries = self
            .workers
            .entries
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?;
        entries.retain(|_, worker| now < worker.expires);
        if let Some((token, worker)) = entries.iter().find(|(_, worker)| {
            worker.resume.as_ref().is_some_and(|scope| scope.key == key)
        }) {
            ensure!(
                !worker.invalid.load(Ordering::Acquire),
                "GOL admission unavailable"
            );
            return Ok(token.clone());
        }
        ensure!(entries.len() < 1024, "GOL runtime capacity reached");
        let token = self.admissions.admit(actor, input, now)?;
        let admission = self
            .admissions
            .resolve(
                &token,
                &request.tenant_id,
                &request.source_object_id,
                now,
            )?
            .context("GOL admission unavailable")?;
        entries.insert(
            token.clone(),
            Arc::new(Worker {
                admission,
                gate: Arc::new(AsyncMutex::new(())),
                invalid: Arc::new(AtomicBool::new(false)),
                facts,
                leases: Mutex::new(BTreeMap::new()),
                dependencies: Mutex::new(receipt.dependencies),
                pins: Mutex::new(origin.pins),
                source_marker: origin.source_marker,
                resume: Some(ResumedScope {
                    key,
                    pipeline: request.pipeline_id,
                    revision: request.pipeline_revision_id,
                    block: request.block_id,
                }),
                expires: now
                    .checked_add(Duration::from_secs(900))
                    .context("GOL runtime unavailable")?,
            }),
        );
        tracing::debug!(event.name = "gol.worker.resume.admitted");
        Ok(token)
    }

    async fn open_worker(
        &self,
        request: RuntimeRequest,
        phase: &mut &'static str,
    ) -> Result<RuntimeLease> {
        let worker = self.worker(
            &request.admission,
            &request.tenant_id,
            &request.source_object_id,
        )?;
        if let Some(scope) = &worker.resume {
            ensure!(
                scope.pipeline == request.pipeline_id &&
                    scope.revision == request.pipeline_revision_id &&
                    scope.block == request.block_id,
                "GOL lineage scope rejected"
            );
        }
        *phase = "boundary";
        let _boundary = self.worker_boundary(&worker).await?;
        *phase = "principal_and_input";
        self.verify_worker(&worker, &request.pipeline_id).await?;
        *phase = "binding";
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
        *phase = "lease";
        let (context_id, lease) = if let Some((id, lease)) = existing {
            ensure!(
                lease.ontology == binding.ontology_id &&
                    lease.binding_pipeline == binding.pipeline_id &&
                    lease.archive.generation().to_string() ==
                        binding.generation &&
                    lease.source_revision == binding.source_revision,
                "GOL binding changed"
            );
            let lease = self.refresh_lease(&worker, &id, lease).await?;
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
        *phase = "dependencies";
        self.recheck_dependencies(&worker, &mut BTreeMap::new())
            .await?;
        *phase = "metadata";
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

    /// Refreshes expired physical readers without resetting calculation state.
    /// Exact-generation open rejects retired artifacts, preserving retention.
    async fn refresh_lease(
        &self,
        worker: &Worker,
        context_id: &str,
        lease: Arc<Lease>,
    ) -> Result<Arc<Lease>> {
        if lease.archive.is_live() {
            return Ok(lease);
        }
        let generation = lease.archive.generation().to_string();
        let archive = self
            .backend()?
            .open(
                &worker.admission.actor.tenant,
                &lease.ontology,
                Some(&generation),
            )
            .await?;
        ensure!(
            archive.provenance().is_some_and(|provenance| {
                provenance.source_revision.as_ref() == lease.source_revision
            }),
            "GOL source binding changed"
        );
        let refreshed = Arc::new(Lease {
            pipeline: lease.pipeline.clone(),
            revision: lease.revision.clone(),
            block: lease.block.clone(),
            binding_pipeline: lease.binding_pipeline.clone(),
            ontology: lease.ontology.clone(),
            source_revision: lease.source_revision.clone(),
            archive: Arc::new(archive),
            targets: lease.targets.clone(),
        });
        worker
            .leases
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
            .insert(context_id.to_owned(), Arc::clone(&refreshed));
        tracing::debug!(event.name = "gol.worker.reader.refreshed");
        Ok(refreshed)
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
        if worker.invalid.load(Ordering::Acquire) {
            tracing::warn!(
                event.name = "gol.worker.invalid",
                reason = "incomplete_effect"
            );
        }
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
        pipeline: &str,
    ) -> Result<()> {
        ensure!(
            !worker.invalid.load(Ordering::Acquire) &&
                Instant::now() < worker.expires,
            "GOL admission unavailable"
        );
        self.verify_worker_source(
            &worker.admission.actor,
            &worker.admission.input,
            &worker.source_marker,
            &worker.facts,
        )
        .await?;
        self.verify_pipeline_execute(worker, pipeline).await
    }

    async fn verify_worker_source(
        &self,
        actor: &Actor,
        input: &VerifiedInput,
        source_marker: &[u8; 32],
        facts: &QueryContext,
    ) -> Result<()> {
        let tenant = actor.tenant.as_ref();
        let user = actor.subject_id.as_ref();
        self.identity.verify_user(tenant, user).await?;
        let source = input
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
            head.e_tag() == Some(input.revision.as_ref()) &&
                metadata.get("tenant").map(String::as_str) == Some(tenant) &&
                metadata.get("owner").map(String::as_str) == Some(user) &&
                metadata.get("authz-origin").map(String::as_str) ==
                    Some("gateway") &&
                metadata.get("authz-gol-admission").is_some_and(|marker| {
                    let digest: [u8; 32] =
                        Sha256::digest(marker.as_bytes()).into();
                    &digest == source_marker
                }),
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
                    Some(facts)
                )
                .await?,
            "GOL input unavailable"
        );
        Ok(())
    }

    async fn verify_pipeline_execute(
        &self,
        worker: &Worker,
        pipeline: &str,
    ) -> Result<()> {
        self.verify_actor_execute(
            &worker.admission.actor,
            &worker.facts,
            pipeline,
        )
        .await
    }

    async fn verify_actor_execute(
        &self,
        actor: &Actor,
        facts: &QueryContext,
        pipeline: &str,
    ) -> Result<()> {
        ensure!(
            self.auth
                .is_authorized(
                    &actor.subject_id,
                    &actor.tenant,
                    Permission::Execute,
                    "pipeline",
                    pipeline,
                    Some(facts)
                )
                .await?,
            "GOL pipeline unavailable"
        );
        Ok(())
    }

    async fn recheck_dependencies(
        &self,
        worker: &Worker,
        archives: &mut BTreeMap<String, RemoteArchive>,
    ) -> Result<BTreeSet<Dependency>> {
        let dependencies = worker
            .dependencies
            .lock()
            .map_err(|_| anyhow::anyhow!("GOL runtime unavailable"))?
            .clone();
        let actor = self
            .actor(
                &worker.admission.actor.tenant,
                &worker.admission.actor.subject_id,
            )
            .await?;
        for dependency in &dependencies {
            self.recheck_dependency(
                &actor,
                &worker.admission.context,
                &worker.facts,
                dependency,
                archives,
            )
            .await?;
        }
        Ok(dependencies)
    }

    /// Releases the selected fields only while the result and all its inputs
    /// are visible to the requesting principal.
    pub(in crate::application::usecases) async fn verify_record_release(
        &self,
        (tenant, user): (&str, &str),
        facts: &QueryContext,
        (instance, key, encoded): (&str, &str, &str),
        value: &serde_json::Value,
        fields: &[&str],
    ) -> Result<()> {
        let backend = self.backend()?;
        let receipt = backend
            .signer
            .as_ref()
            .context("GOL provenance unavailable")?
            .verify(encoded, tenant, instance, value)?;
        let Reference::Symbol(symbol) = &receipt.target else {
            anyhow::bail!("GOL release requires a record");
        };
        ensure!(
            reference_key(&receipt.target) == key,
            "GOL release target mismatch"
        );
        let (module, _) = key
            .split_once("::")
            .context("GOL release target unavailable")?;
        let ontology = backend
            .authority
            .module_owners
            .get(&module.parse()?)
            .map_or_else(|| module.to_owned(), ToString::to_string);
        let actor = self.actor(tenant, user).await?;
        let state = Arc::new(ExecutionContext::from_inputs(
            &actor.principal(),
            actor.clearance.clone(),
            receipt.flow.join(&backend.profile.context_floor),
        )?);
        let mut archives = BTreeMap::new();
        for dependency in &receipt.dependencies {
            self.recheck_dependency(
                &actor,
                &state,
                facts,
                dependency,
                &mut archives,
            )
            .await?;
        }
        let mut dependency = Dependency {
            ontology,
            target: key.to_owned(),
            instance_id: receipt.instance_id,
            revision: receipt.source_revision,
            operation: Operation::Read,
        };
        self.recheck_dependency(
            &actor,
            &state,
            facts,
            &dependency,
            &mut archives,
        )
        .await?;
        for field in fields {
            ensure!(
                value.get(field).is_some(),
                "GOL release field unavailable"
            );
            dependency.target = reference_key(
                &galadril_ontology::FieldRef::new(
                    symbol.clone(),
                    field.parse()?,
                )
                .into(),
            );
            self.recheck_dependency(
                &actor,
                &state,
                facts,
                &dependency,
                &mut archives,
            )
            .await?;
        }
        Ok(())
    }

    async fn recheck_dependency(
        &self,
        actor: &Actor,
        state: &Arc<ExecutionContext>,
        facts: &QueryContext,
        dependency: &Dependency,
        archives: &mut BTreeMap<String, RemoteArchive>,
    ) -> Result<()> {
        let backend = self.backend()?;
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
                            Some(facts)
                        )
                        .await?,
                "GOL source provenance unavailable"
            );
            return Ok(());
        }
        // All dependency checks in this request share a bounded active
        // archive. SpiceDB and Cedar still decide every concrete obligation.
        let archive = match archives.entry(dependency.ontology.clone()) {
            std::collections::btree_map::Entry::Occupied(entry) => {
                entry.into_mut()
            },
            std::collections::btree_map::Entry::Vacant(entry) => entry.insert(
                backend
                    .open(&actor.tenant, &dependency.ontology, None)
                    .await?,
            ),
        };
        let target = resolve(archive, &dependency.target)?;
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
                archive,
                &access,
                actor,
                Arc::clone(state),
                facts,
                &backend.authority,
                &backend.profile.context_floor,
            )
            .await?
        } else {
            snapshot::Snapshot::prepare_data(
                archive,
                &access,
                actor,
                Arc::clone(state),
                facts,
                &backend.authority,
                &backend.profile.context_floor,
                &dependency.revision,
                None,
            )
            .await?
        };
        let decision = archive
            .authorize(&access, &prepared, &backend.provider)
            .await;
        if let Decision::Deny(reason) = decision {
            tracing::warn!(
                event.name = "gol.worker.dependency.denied",
                reason = ?reason,
                metadata = dependency.instance_id == "__schema"
            );
        }
        ensure!(decision == Decision::Allow, "GOL dependency unavailable");
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
            EffectRequest::Begin(request) => {
                let mut phase = "admission";
                self.begin_effects(request, &mut phase).await.map_err(|_| {
                    tracing::warn!(
                        event.name = "gol.worker.effect.denied",
                        phase
                    );
                    RuntimeError::Denied
                })
            },
        }
    }

    async fn begin_effects(
        &self,
        request: BeginEffects,
        phase: &mut &'static str,
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
        self.verify_worker(&worker, &request.pipeline_id).await?;
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
        let lease = self
            .refresh_lease(&worker, &request.context_id, lease)
            .await?;
        *phase = "dependencies";
        let mut archives = BTreeMap::new();
        let mut rechecked =
            self.recheck_dependencies(&worker, &mut archives).await?;
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
            *phase = "provenance";
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
            if let Some(handoff) = &effect.handoff {
                ensure!(
                    operation == Operation::Transfer &&
                        effect.destination == "kafka" &&
                        effect.purpose == "pipeline_dispatch" &&
                        effect.instance_id.is_none() &&
                        effect.proposal.is_none() &&
                        effect.receipt.is_none() &&
                        !handoff.block_id.is_empty() &&
                        handoff.block_id.len() <= 128 &&
                        !handoff.command_id.is_nil(),
                    "GOL command handoff requires a broker release"
                );
            }
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
            let projected = if effect.read_fields.is_some() {
                ensure!(
                    matches!(target, Reference::Symbol(_)),
                    "GOL record projection requires a symbol"
                );
                Some(effect.read_fields()?)
            } else {
                None
            };
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
                let before = worker.admission.context.current_label()?;
                worker.admission.context.include_input(&receipt.flow)?;
                if worker.admission.context.current_label()? != before {
                    rechecked = self
                        .recheck_dependencies(&worker, &mut archives)
                        .await?;
                }
                for dependency in &receipt.dependencies {
                    if rechecked.insert(dependency.clone()) {
                        self.recheck_dependency(
                            &actor,
                            &worker.admission.context,
                            &worker.facts,
                            dependency,
                            &mut archives,
                        )
                        .await?;
                    }
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
            if effect.destination == "identity_state" {
                // Every persisted prototype is a concrete GOL value. Reads
                // require authenticated provenance, then the ordinary live
                // policy and IFC gates below still decide its disclosure.
                ensure!(
                    operation == Operation::Write || receipt.is_some(),
                    "GOL identity provenance unavailable"
                );
            }
            if effect.destination == "causal_state" &&
                (!matches!(target, Reference::Symbol(_)) ||
                    (operation != Operation::Write &&
                        !(operation == Operation::Read &&
                            receipt.is_some())))
            {
                worker
                    .admission
                    .context
                    .include_input(&backend.profile.shared_state_floor)?;
                anyhow::bail!("GOL causal-state provenance unavailable");
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
            if operation == Operation::Read &&
                receipt.is_some() &&
                matches!(target, Reference::Symbol(_))
            {
                // Root access does not confer access to the consumed fields.
                let fields = match projected {
                    Some(fields) => fields,
                    None => effect.read_fields()?,
                };
                for name in fields {
                    let mut field_access =
                        access_clone(&access, Operation::Read);
                    field_access.route.target =
                        galadril_ontology::FieldRef::new(
                            owner(&target)?.clone(),
                            name.parse()?,
                        )
                        .into();
                    for archive in [lease.archive.as_ref(), &current] {
                        let field = resolve(
                            archive,
                            &reference_key(&field_access.route.target),
                        )?;
                        for descriptor in archive
                            .requirements(&Route::new(field, Operation::Read))
                            .await?
                            .values()
                        {
                            outcome = outcome
                                .join(descriptor.flow())
                                .join(descriptor.control_flow());
                        }
                    }
                    checked.push(field_access);
                }
            }
            if operation == Operation::Write {
                *phase = "write_instance";
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
                *phase = "write_contract";
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
                    .await
                    .inspect_err(|error| {
                        tracing::warn!(
                            event.name = "gol.worker.write_contract.denied",
                            reason = ?error
                        );
                    })?;
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
        *phase = "write_visibility";
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
        *phase = "outcome_flow";
        worker.admission.context.include_input(&outcome)?;
        for access in &checked {
            *phase = "pinned_snapshot";
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
            *phase = "pinned_policy";
            if let Decision::Deny(reason) = lease
                .archive
                .authorize(access, &prepared, &backend.provider)
                .await
            {
                tracing::warn!(
                    event.name = "gol.worker.policy.denied",
                    archive = "pinned",
                    reader_live = lease.archive.is_live(),
                    operation = ?access.route.operation,
                    reason = ?reason,
                );
                anyhow::bail!("GOL effect denied");
            }
            // A newly published policy must also approve an already pinned
            // type reader.
            *phase = "active_snapshot";
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
            *phase = "active_policy";
            if let Decision::Deny(reason) = current
                .authorize(
                    &current_access,
                    &current_snapshot,
                    &backend.provider,
                )
                .await
            {
                tracing::warn!(
                    event.name = "gol.worker.policy.denied",
                    archive = "active",
                    reader_live = current.is_live(),
                    operation = ?access.route.operation,
                    reason = ?reason,
                );
                anyhow::bail!("GOL active policy denied");
            }
        }
        *phase = "read_provenance";
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
        *phase = "provenance_budget";
        ensure!(dependencies.len() <= 1024, "GOL provenance budget exceeded");
        *phase = "release";
        let lineage =
            if request.effects.iter().zip(&roots).any(|(effect, access)| {
                access.route.operation == Operation::Write ||
                    effect.handoff.is_some()
            }) {
                Some(LineageOrigin {
                    issuer: actor.issuer.to_string(),
                    subject_id: actor.subject_id.to_string(),
                    source_object_id: worker
                        .admission
                        .input
                        .source_object_id
                        .to_string(),
                    source_revision: worker
                        .admission
                        .input
                        .revision
                        .to_string(),
                    source_marker: worker.source_marker,
                    pipeline_id: request.pipeline_id.clone(),
                    pipeline_revision_id: request.pipeline_revision_id.clone(),
                    pins: worker
                        .pins
                        .lock()
                        .map_err(|_| {
                            anyhow::anyhow!("GOL runtime unavailable")
                        })?
                        .clone(),
                    handoff: None,
                })
            } else {
                None
            };
        let receipts = request
            .effects
            .iter()
            .zip(&roots)
            .map(|(effect, access)| {
                let mut handoff_lineage;
                let (value, lineage) = if let Some(handoff) = &effect.handoff {
                    handoff_lineage =
                        lineage.clone().context("GOL lineage unavailable")?;
                    handoff_lineage.handoff = Some(handoff.clone());
                    (&serde_json::Value::Null, Some(&handoff_lineage))
                } else if access.route.operation == Operation::Write {
                    (
                        effect
                            .proposal
                            .as_ref()
                            .context("GOL proposal required")?,
                        lineage.as_ref(),
                    )
                } else {
                    return Ok(None);
                };
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
                            lineage,
                        },
                        &flow,
                        value,
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
