//! Registry-owned GOL sources and atomic activation of checked artifacts.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use chrono::Utc;
use galadril_ontology::archive::remote::{
    Generation, PublicationProvenance, RemoteOptions, RemoteStore,
};
use galadril_ontology::archive::{
    ArchiveError, CompiledArchive, compile_archive,
};
use galadril_ontology::semantic::Environment;
use galadril_ontology::syntax::{Declaration, ImportKind, parse_source};
use galadril_ontology::{
    ExternalTypeRegistry, ModuleVersion, Name, OntologyId, SymbolRef,
};
use moka::future::Cache;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::domain::{Registry, validate_identity};
use crate::gol::{
    GolAuthorityConfig, GolDiagnostic, GolFile, analyze_files, validate_files,
};
use crate::state::{GolBindingRecord, GolSourceRecord, TenantState};
use crate::storage::{RevisionStore, valid_revision};

/// Trusted artifact dependencies, separate from authored source payloads.
#[derive(Clone)]
pub struct GolPublicationConfig {
    client: aws_sdk_s3::Client,
    bucket: Arc<str>,
    authority: Arc<GolAuthorityConfig>,
    external_types: Arc<ExternalTypeRegistry>,
    stores: Cache<(String, String), RemoteStore>,
    metadata: Arc<dyn MetadataProvisioner>,
}

/// Trusted relationship writer invoked before an artifact can become active.
#[async_trait::async_trait]
pub trait MetadataProvisioner: Send + Sync {
    /// Grants only the authenticated publisher's reserved schema instances.
    async fn provision(
        &self,
        tenant: &str,
        ontology: &str,
        author_id: &str,
        candidates: &[SymbolRef],
    ) -> Result<()>;
}

impl GolPublicationConfig {
    /// Installs explicit deployment approvals without granting runtime access.
    pub fn new(
        client: aws_sdk_s3::Client,
        bucket: impl Into<Arc<str>>,
        authority: GolAuthorityConfig,
        external_types: ExternalTypeRegistry,
        metadata: Arc<dyn MetadataProvisioner>,
    ) -> Result<Self> {
        let bucket = bucket.into();
        ensure!(!bucket.is_empty(), "GOL artifact bucket is required");
        Ok(Self {
            client,
            bucket,
            authority: Arc::new(authority),
            external_types: Arc::new(external_types),
            stores: Cache::builder().max_capacity(1024).build(),
            metadata,
        })
    }

    async fn store(
        &self,
        tenant: &str,
        ontology: &str,
    ) -> Result<RemoteStore> {
        let key = (tenant.to_owned(), ontology.to_owned());
        if let Some(store) = self.stores.get(&key).await {
            return Ok(store);
        }
        let namespace: Name =
            format!("t_{:x}", Sha256::digest(tenant.as_bytes())).parse()?;
        let store = RemoteStore::connect_namespace(
            self.client.clone(),
            &self.bucket,
            tenant,
            namespace,
            ontology.parse()?,
            RemoteOptions::default(),
        )
        .await?;
        self.stores.insert(key, store.clone()).await;
        Ok(store)
    }
}

/// Bounded source-authoring input; syntax errors are intentionally saveable.
pub struct SaveGolOntology<'a> {
    /// Authenticated tenant selected by Gateway.
    pub tenant_id: &'a str,
    /// Qualified logical identity of the package's primary module.
    pub ontology_id: &'a str,
    /// Catalog display name, available only through catalog permissions.
    pub display_name: &'a str,
    /// Complete replacement source package.
    pub files: Vec<GolFile>,
    /// Authenticated author supplied by Gateway.
    pub author: &'a str,
    /// Required immutable tenant head for replacement; absent creates only.
    pub expected_revision: Option<&'a str>,
    /// Supplied only for installation from an operator-deployed plugin.
    pub origin: Option<GolPackageOrigin>,
}

/// Catalog metadata without authored source or symbol names.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GolSummary {
    /// Logical ontology package identity.
    pub ontology_id: String,
    /// Human-readable catalog name.
    pub display_name: String,
    /// Immutable lakeFS tenant snapshot containing this source package.
    pub source_revision: String,
    /// Generation selected exclusively by the RustFS manifest.
    pub active_generation: Option<String>,
    /// Source revision pinned by the active RustFS manifest.
    pub active_source_revision: Option<String>,
    /// `draft` or `published`; validation is reported independently.
    pub status: String,
    /// Origin of the installed base source, retained through tenant edits.
    #[serde(default)]
    pub origin: Option<GolPackageOrigin>,
}

/// Identity of the deployed package from which a tenant source was installed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GolPackageOrigin {
    /// Deployment manifest namespace.
    pub plugin_id: String,
    /// Version of the deployment manifest.
    pub plugin_version: String,
    /// Package release version; individual modules declare their own
    /// versions.
    pub package_version: String,
}

impl GolPackageOrigin {
    /// Bounds metadata before persisting it in tenant-controlled state.
    pub fn validate(&self) -> Result<()> {
        let id = self.plugin_id.as_bytes();
        ensure!(
            id.len() <= 64 &&
                id.first().is_some_and(u8::is_ascii_lowercase) &&
                id.iter().all(|byte| byte.is_ascii_lowercase() ||
                    byte.is_ascii_digit() ||
                    matches!(byte, b'_' | b'-')),
            "invalid GOL plugin identity"
        );
        for version in [&self.plugin_version, &self.package_version] {
            ensure!(
                !version.is_empty() &&
                    version.len() <= 64 &&
                    version
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() ||
                            matches!(byte, b'.' | b'-' | b'+' | b'_')),
                "invalid GOL package version"
            );
        }
        Ok(())
    }
}

/// A module dependency pinned through an active immutable generation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GolDependency {
    /// Imported module identity.
    pub ontology_id: String,
    /// Published generation used during compilation.
    pub generation: String,
    /// Immutable source revision represented by that generation.
    pub source_revision: String,
}

/// Private, incrementally disclosed source-package dependency plan.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GolDependencyPreview {
    /// Package owners whose sources have not yet been authorized.
    pub required_owners: Vec<String>,
    /// Exact immutable plan, absent until every required owner is admitted.
    pub plan_pin: Option<String>,
    /// Version pins disclosed only after authorizing their source packages.
    pub dependencies: Vec<GolDependency>,
}

/// Trusted Gateway approval, never accepted from GraphQL arguments.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GolCompileApproval {
    /// Fingerprint of source, dependency generations and host approvals.
    pub plan_pin: String,
    /// Source-package identities admitted by the live authoring gate.
    pub approved_owners: Vec<String>,
}

struct CompilationSources {
    files: Vec<GolFile>,
    modules: BTreeSet<String>,
    dependencies: Vec<GolDependency>,
    required_owners: BTreeSet<String>,
    cyclic_publication: bool,
}

/// Source workspace, disclosed only to authorized source authors.
#[derive(Debug, Serialize, Deserialize)]
pub struct GolWorkspace {
    /// Source and active-generation metadata.
    pub summary: GolSummary,
    /// Complete source package in deterministic path order.
    pub files: Vec<GolFile>,
    /// Last requested compilation diagnostics; workspace reads do not
    /// compile.
    pub diagnostics: Vec<GolDiagnostic>,
    /// Dependencies are populated by explicit validation.
    pub dependencies: Vec<GolDependency>,
}

/// Compilation outcome bound to the exact submitted immutable revision.
#[derive(Debug, Serialize, Deserialize)]
pub struct GolValidation {
    /// Logical ontology package identity.
    pub ontology_id: String,
    /// Immutable source revision that was checked.
    pub source_revision: String,
    /// `valid`, `invalid`, or `pending_authority`.
    pub status: String,
    /// Source-localized compiler or approval diagnostics.
    pub diagnostics: Vec<GolDiagnostic>,
    /// Exact dependency selections used by this validation.
    pub dependencies: Vec<GolDependency>,
}

/// Publication receipt from the atomic RustFS manifest update.
#[derive(Debug, Serialize, Deserialize)]
pub struct GolPublication {
    /// Logical ontology identity.
    pub ontology_id: String,
    /// Immutable lakeFS source revision.
    pub source_revision: String,
    /// Newly activated generation.
    pub generation: String,
}

struct Prepared {
    cyclic_publication: bool,
    archive: CompiledArchive,
    dependencies: Vec<GolDependency>,
    candidates: Vec<SymbolRef>,
    modules: BTreeSet<ModuleVersion>,
}

impl<S: RevisionStore> Registry<S> {
    /// Enables publication with explicitly approved host configuration.
    #[must_use]
    pub fn with_gol(mut self, config: GolPublicationConfig) -> Self {
        self.gol = Some(config);
        self
    }

    /// Commits raw source files, retaining invalid drafts for correction.
    #[tracing::instrument(skip_all, fields(operation = "gol.save"))]
    pub async fn save_gol_ontology(
        &self,
        input: SaveGolOntology<'_>,
    ) -> Result<GolWorkspace> {
        validate_gol_identity(input.tenant_id, input.ontology_id)?;
        let _mutation = self.gol_mutation(input.tenant_id).await?;
        ensure!(
            !input.display_name.trim().is_empty() &&
                input.display_name.len() <= 256,
            "invalid ontology display name"
        );
        ensure!(
            !input.author.is_empty() && input.author.len() <= 256,
            "invalid ontology author"
        );
        let source_digest = crate::gol::source_digest(&input.files)?;
        if let Some(origin) = &input.origin {
            origin.validate()?;
        }
        self.store.ensure_tenant(input.tenant_id).await?;
        let snapshot = self.store.load(input.tenant_id, None).await?;
        let mut state = snapshot.state;
        let existing = state.ontologies.get(input.ontology_id);
        ensure!(
            existing.is_none_or(|record| record.deletion_base.is_none()),
            "ontology deletion is pending"
        );
        if let (Some(origin), Some(record)) = (&input.origin, existing) &&
            record.origin.as_ref() == Some(origin) &&
            record.source_digest == source_digest
        {
            tracing::debug!(event.name = "registry.gol.install_reused");
            return self
                .gol_workspace(
                    input.tenant_id,
                    input.ontology_id,
                    Some(&snapshot.revision_id),
                )
                .await;
        }
        if let Some(expected) = input.expected_revision {
            ensure!(
                valid_revision(expected) && expected == snapshot.revision_id,
                "registry revision changed"
            );
            ensure!(existing.is_some(), "ontology unavailable");
        } else {
            ensure!(
                existing.is_none(),
                "expected revision is required for existing ontology"
            );
        }
        let package_source_digest = match (&input.origin, existing) {
            (Some(origin), Some(record)) => {
                ensure!(
                    record
                        .origin
                        .as_ref()
                        .is_some_and(|previous| previous.plugin_id ==
                            origin.plugin_id &&
                            previous != origin) &&
                        record.package_source_digest.as_ref() ==
                            Some(&record.source_digest),
                    "installed GOL sources differ from the selected plugin package"
                );
                Some(source_digest.clone())
            },
            (Some(_), None) => Some(source_digest.clone()),
            (None, record) => {
                record.and_then(|record| record.package_source_digest.clone())
            },
        };
        // A source upgrade leaves the tenant's catalog name intact.
        let display_name = match (input.origin.as_ref(), existing) {
            (Some(_), Some(record)) => record.display_name.clone(),
            _ => input.display_name.into(),
        };
        // A draft keeps the publication baseline the editor needs for its
        // next compare-and-swap. Read it before committing the draft.
        let (active_generation, active_source_revision) = match existing {
            Some(record) => {
                let summary = self
                    .gol_summary(
                        input.tenant_id,
                        &snapshot.revision_id,
                        record,
                    )
                    .await?;
                (summary.active_generation, summary.active_source_revision)
            },
            None => (None, None),
        };
        let now = Utc::now().timestamp_millis();
        let created_at_ms =
            existing.map_or(now, |record| record.created_at_ms);
        let origin = input
            .origin
            .or_else(|| existing.and_then(|record| record.origin.clone()));
        let mut files = input.files;
        files.sort_unstable_by(|left, right| left.path.cmp(&right.path));
        let (declared_modules, imports) = match source_imports(&files) {
            Ok((modules, imports)) => (modules, Some(imports)),
            Err(_diagnostics) => {
                (BTreeSet::from([input.ontology_id.to_owned()]), None)
            },
        };
        state.ontologies.insert(
            input.ontology_id.into(),
            GolSourceRecord {
                ontology_id: input.ontology_id.into(),
                display_name,
                paths: files.iter().map(|file| file.path.clone()).collect(),
                source_digest,
                declared_modules,
                imports,
                files,
                author: input.author.into(),
                created_at_ms,
                updated_at_ms: now,
                deletion_base: None,
                origin,
                package_source_digest,
            },
        );
        let revision = self
            .store
            .commit(
                input.tenant_id,
                &snapshot.revision_id,
                &state,
                input.author,
                "Save GOL sources",
            )
            .await?;
        let record = state
            .ontologies
            .remove(input.ontology_id)
            .context("saved GOL source is missing")?;
        tracing::info!(event.name="registry.gol.source_saved",ontology_id=input.ontology_id,source_revision=%revision);
        Ok(GolWorkspace {
            summary: GolSummary {
                ontology_id: record.ontology_id,
                display_name: record.display_name,
                source_revision: revision,
                active_generation,
                active_source_revision,
                status: "draft".into(),
                origin: record.origin,
            },
            files: record.files,
            diagnostics: Vec::new(),
            dependencies: Vec::new(),
        })
    }

    /// Reads sources at one immutable tenant revision without compilation.
    pub async fn gol_workspace(
        &self,
        tenant: &str,
        ontology: &str,
        revision: Option<&str>,
    ) -> Result<GolWorkspace> {
        validate_gol_identity(tenant, ontology)?;
        let mut snapshot = self.store.load(tenant, revision).await?;
        let record = snapshot
            .state
            .ontologies
            .remove(ontology)
            .filter(|record| record.deletion_base.is_none())
            .context("ontology unavailable")?;
        let summary = self
            .gol_summary(tenant, &snapshot.revision_id, &record)
            .await?;
        Ok(GolWorkspace {
            summary,
            files: self
                .store
                .load_gol_sources(tenant, &snapshot.revision_id, ontology)
                .await?,
            diagnostics: Vec::new(),
            dependencies: Vec::new(),
        })
    }

    /// Lists catalog records; no source, property, or symbol names are sent.
    pub async fn list_gol_ontologies(
        &self,
        tenant: &str,
        limit: usize,
    ) -> Result<Vec<GolSummary>> {
        ensure!(crate::storage::valid_tenant(tenant), "invalid tenant ID");
        // First-time browsing must not create a lakeFS repository.
        if !self.store.tenant_exists(tenant).await? {
            return Ok(Vec::new());
        }
        let snapshot = self.store.load(tenant, None).await?;
        let mut result = Vec::with_capacity(
            snapshot.state.ontologies.len().min(limit).min(100),
        );
        for record in snapshot
            .state
            .ontologies
            .values()
            .filter(|record| record.deletion_base.is_none())
            .take(limit.min(100))
        {
            result.push(
                self.gol_summary(tenant, &snapshot.revision_id, record)
                    .await?,
            );
        }
        Ok(result)
    }

    async fn gol_summary(
        &self,
        tenant: &str,
        revision: &str,
        record: &GolSourceRecord,
    ) -> Result<GolSummary> {
        let mut summary = GolSummary {
            ontology_id: record.ontology_id.clone(),
            display_name: record.display_name.clone(),
            source_revision: revision.into(),
            active_generation: None,
            active_source_revision: None,
            status: "draft".into(),
            origin: record.origin.clone(),
        };
        if let Some(config) = &self.gol {
            let store = config.store(tenant, &record.ontology_id).await?;
            if store.active().await?.is_some() {
                let archive = store.open().await?;
                summary.active_generation =
                    Some(archive.generation().to_string());
                summary.active_source_revision = Some(
                    archive
                        .provenance()
                        .context("published GOL provenance unavailable")?
                        .source_revision
                        .to_string(),
                );
                summary.status = "published".into();
            }
        }
        Ok(summary)
    }

    async fn source_record(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
    ) -> Result<GolSourceRecord> {
        let mut snapshot = self.store.load(tenant, Some(revision)).await?;
        let mut record = snapshot
            .state
            .ontologies
            .remove(ontology)
            .filter(|record| record.deletion_base.is_none())
            .context("ontology unavailable")?;
        record.files = self
            .store
            .load_gol_sources(tenant, revision, ontology)
            .await?;
        Ok(record)
    }

    /// Discovers only imports of source packages already admitted by Gateway.
    pub async fn preview_gol_dependencies(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
        approved_owners: &[String],
    ) -> Result<GolDependencyPreview> {
        validate_gol_identity(tenant, ontology)?;
        ensure!(valid_revision(revision), "invalid source revision");
        validate_approved_owners(approved_owners)?;
        let record = self.source_record(tenant, ontology, revision).await?;
        match self
            .compilation_sources(tenant, ontology, &record, approved_owners)
            .await
        {
            Ok(sources) => Ok(GolDependencyPreview {
                plan_pin: if sources.required_owners.is_empty() {
                    Some(self.compilation_pin(
                        tenant,
                        ontology,
                        revision,
                        &sources.dependencies,
                    )?)
                } else {
                    None
                },
                required_owners: sources.required_owners.into_iter().collect(),
                dependencies: sources.dependencies,
            }),
            // Malformed admitted source still needs its precise parser
            // diagnostics.
            Err(_diagnostics) => Ok(GolDependencyPreview {
                required_owners: Vec::new(),
                plan_pin: Some(self.compilation_pin(
                    tenant,
                    ontology,
                    revision,
                    &[],
                )?),
                dependencies: Vec::new(),
            }),
        }
    }

    fn compilation_pin(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
        dependencies: &[GolDependency],
    ) -> Result<String> {
        let host = self.gol.as_ref().map(|config| config.authority.as_ref());
        let bytes = serde_json::to_vec(&(
            tenant,
            ontology,
            revision,
            dependencies,
            host,
        ))?;
        Ok(format!("{:x}", Sha256::digest(bytes)))
    }

    fn require_compilation_plan(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
        dependencies: &[GolDependency],
        approval: &GolCompileApproval,
    ) -> Result<()> {
        validate_approved_owners(&approval.approved_owners)?;
        ensure!(
            approval.plan_pin.len() == 64 &&
                approval.plan_pin ==
                    self.compilation_pin(
                        tenant,
                        ontology,
                        revision,
                        dependencies
                    )?,
            "GOL dependency plan changed"
        );
        Ok(())
    }

    /// Validates source, imports, and security without activating artifacts.
    pub async fn validate_gol_ontology(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
        approval: &GolCompileApproval,
    ) -> Result<GolValidation> {
        validate_gol_identity(tenant, ontology)?;
        ensure!(valid_revision(revision), "invalid source revision");
        let record = self.source_record(tenant, ontology, revision).await?;
        let (status, diagnostics, dependencies) = match self
            .prepare(tenant, ontology, revision, &record, approval)
            .await
        {
            Ok(prepared) => ("valid", Vec::new(), prepared.dependencies),
            Err(diagnostics) => {
                let pending = diagnostics
                    .iter()
                    .any(|item| item.code == "gol::authority_unavailable");
                (
                    if pending {
                        "pending_authority"
                    } else {
                        "invalid"
                    },
                    diagnostics,
                    Vec::new(),
                )
            },
        };
        Ok(GolValidation {
            ontology_id: ontology.into(),
            source_revision: revision.into(),
            status: status.into(),
            diagnostics,
            dependencies,
        })
    }

    /// Compiles the pinned revision and atomically activates its whole
    /// archive.
    #[tracing::instrument(skip_all, fields(operation = "gol.publish"))]
    pub async fn publish_gol_ontology(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
        expected_generation: Option<&str>,
        author_id: &str,
        approval: &GolCompileApproval,
    ) -> Result<GolPublication> {
        validate_gol_identity(tenant, ontology)?;
        ensure!(
            !author_id.is_empty() && author_id.len() <= 256,
            "authenticated publisher is required"
        );
        let _mutation = self.gol_mutation(tenant).await?;
        ensure!(valid_revision(revision), "invalid source revision");
        let record = self.source_record(tenant, ontology, revision).await?;
        let prepared=self.prepare(tenant,ontology,revision,&record,approval).await.map_err(|_diagnostics| {
            anyhow::anyhow!("GOL validation failed; validate the source revision for diagnostics")
        })?;
        if prepared.cyclic_publication {
            return Err(PublicationCycle.into());
        }
        let config = self
            .gol
            .as_ref()
            .context("approved GOL publication configuration unavailable")?;
        ensure!(
            self.store
                .load(tenant, None)
                .await?
                .state
                .ontologies
                .get(ontology)
                .is_some_and(|record| record.deletion_base.is_none()),
            "ontology unavailable"
        );
        let store = config.store(tenant, ontology).await?;
        let expected = expected_generation
            .map(str::parse::<Generation>)
            .transpose()?;
        ensure!(store.active().await? == expected, "GOL generation changed");
        let mut pins = BTreeMap::new();
        for dependency in prepared.dependencies {
            let id: OntologyId = dependency.ontology_id.parse()?;
            let generation: Generation = dependency.generation.parse()?;
            ensure!(
                config
                    .store(tenant, &dependency.ontology_id)
                    .await?
                    .active()
                    .await? ==
                    Some(generation),
                "GOL dependency generation changed"
            );
            pins.insert(id, generation);
        }
        let provenance = PublicationProvenance {
            source_revision: revision.into(),
            dependencies: pins,
            candidates: prepared.candidates,
            modules: prepared.modules,
        };
        let staged = store
            .stage_with_provenance(&prepared.archive, expected, provenance)
            .await?;
        ensure!(
            self.store
                .load(tenant, None)
                .await?
                .state
                .ontologies
                .get(ontology)
                .is_some_and(|record| record.deletion_base.is_none()),
            "ontology unavailable"
        );
        let provenance = staged
            .provenance()
            .context("staged GOL provenance unavailable")?;
        config
            .metadata
            .provision(tenant, ontology, author_id, &provenance.candidates)
            .await?;
        let generation = staged.activate().await?;
        tracing::info!(event.name="registry.gol.published",ontology_id=ontology,generation=%generation);
        Ok(GolPublication {
            ontology_id: ontology.into(),
            source_revision: revision.into(),
            generation: generation.to_string(),
        })
    }

    /// Fences source edits, retires the artifact, then finalizes removal.
    pub async fn delete_gol_ontology(
        &self,
        tenant: &str,
        ontology: &str,
        expected: &str,
        author: &str,
    ) -> Result<String> {
        validate_gol_identity(tenant, ontology)?;
        let _mutation = self.gol_mutation(tenant).await?;
        ensure!(!author.is_empty(), "ontology author required");
        let mut snapshot = self.store.load(tenant, None).await?;
        let record = snapshot
            .state
            .ontologies
            .get(ontology)
            .context("ontology unavailable")?;
        if let Some(base) = &record.deletion_base {
            ensure!(base == expected, "registry revision changed");
        } else {
            ensure!(
                snapshot.revision_id == expected,
                "registry revision changed"
            );
            ensure!(
                !snapshot
                    .state
                    .bindings
                    .values()
                    .any(|binding| binding.ontology_id == ontology),
                "ontology has pipeline dependents"
            );
            self.ensure_no_deployed_gol_dependents(
                tenant,
                ontology,
                &snapshot.state,
            )
            .await?;
            let mut owned_modules = record.declared_modules.clone();
            owned_modules.insert(ontology.to_owned());
            if let Some(config) = &self.gol {
                for (module, owner) in &config.authority.module_owners {
                    if owner.to_string() == ontology {
                        owned_modules.insert(module.to_string());
                    }
                }
            }
            for other in snapshot.state.ontologies.values().filter(|record| {
                record.ontology_id != ontology &&
                    record.deletion_base.is_none()
            }) {
                let imports = other
                    .imports
                    .as_ref()
                    .context("ontology dependency state is unresolved")?;
                ensure!(
                    imports.is_disjoint(&owned_modules),
                    "ontology has source dependents"
                );
            }
            snapshot
                .state
                .ontologies
                .get_mut(ontology)
                .context("ontology unavailable")?
                .deletion_base = Some(expected.into());
            self.store
                .commit(
                    tenant,
                    expected,
                    &snapshot.state,
                    author,
                    "Fence GOL source deletion",
                )
                .await?;
        }
        if let Some(config) = &self.gol {
            let store = config.store(tenant, ontology).await?;
            store.deactivate(store.active().await?).await?;
        }
        let mut current = self.store.load(tenant, None).await?;
        let record = current
            .state
            .ontologies
            .get(ontology)
            .context("ontology unavailable")?;
        ensure!(
            record.deletion_base.as_deref() == Some(expected),
            "ontology deletion changed"
        );
        current
            .state
            .ontologies
            .remove(ontology)
            .context("ontology unavailable")?;
        self.store
            .commit(
                tenant,
                &current.revision_id,
                &current.state,
                author,
                "Complete GOL source deletion",
            )
            .await
    }

    /// Old pipeline revisions can keep a binding after a draft removes it.
    async fn ensure_no_deployed_gol_dependents(
        &self,
        tenant: &str,
        ontology: &str,
        current: &TenantState,
    ) -> Result<()> {
        let mut pending = current
            .pipelines
            .values()
            .filter(|pipeline| pipeline.deleted_at_ms.is_none())
            .filter_map(|pipeline| {
                Some((
                    pipeline.pipeline_id.clone(),
                    pipeline.published_revision_id.as_ref()?.clone(),
                ))
            })
            .collect::<Vec<_>>();
        let mut visited = BTreeSet::new();
        while let Some((pipeline, revision)) = pending.pop() {
            if !visited.insert((pipeline.clone(), revision.clone())) {
                continue;
            }
            let snapshot = self.store.load(tenant, Some(&revision)).await?;
            if snapshot.state.bindings.values().any(|binding| {
                binding.pipeline_id == pipeline &&
                    binding.ontology_id == ontology
            }) {
                tracing::warn!(
                    event.name = "registry.gol.deployed_dependency"
                );
                anyhow::bail!("ontology has pipeline dependents");
            }
            let record = snapshot
                .state
                .pipelines
                .get(&pipeline)
                .filter(|record| record.deleted_at_ms.is_none())
                .context("deployed pipeline dependency unavailable")?;
            pending.extend(
                crate::composition::references(&record.definition)?
                    .into_iter()
                    .map(|reference| {
                        (reference.pipeline_id, reference.revision_id)
                    }),
            );
        }
        Ok(())
    }

    async fn gol_mutation(
        &self,
        tenant: &str,
    ) -> Result<tokio::sync::MutexGuard<'_, ()>> {
        let digest = Sha256::digest(tenant.as_bytes());
        let stripe = usize::from(
            digest.first().copied().context("tenant digest is empty")?,
        ) % self.gol_locks.len();
        Ok(self
            .gol_locks
            .get(stripe)
            .context("GOL mutation lock is unavailable")?
            .lock()
            .await)
    }

    async fn compilation_sources(
        &self,
        tenant: &str,
        ontology: &str,
        record: &GolSourceRecord,
        approved_owners: &[String],
    ) -> Result<CompilationSources, Vec<GolDiagnostic>> {
        let (modules, mut pending) = source_imports(&record.files)?;
        if !modules.contains(ontology) {
            return Err(vec![diagnostic(
                "gol::module_identity",
                "Source package must declare its primary ontology identity",
            )]);
        }
        if let Some(config) = &self.gol {
            for module in &modules {
                let id = module.parse::<OntologyId>().map_err(|_| {
                    vec![diagnostic(
                        "gol::module_identity",
                        "Invalid module identity",
                    )]
                })?;
                let owner = config
                    .authority
                    .module_owners
                    .get(&id)
                    .map(ToString::to_string);
                if owner.as_deref().unwrap_or(module.as_str()) != ontology {
                    return Err(vec![diagnostic(
                        "gol::module_ownership",
                        "Source package does not own this companion module",
                    )]);
                }
            }
        }
        pending.retain(|import| !modules.contains(import));
        let mut loaded = modules.clone();
        let mut files = record.files.clone();
        let mut dependencies = BTreeMap::<String, GolDependency>::new();
        let mut required_owners = BTreeSet::new();
        let mut expected_pins = BTreeMap::<String, Generation>::new();
        let mut cyclic_publication = false;
        while let Some(import) = pending.pop_first() {
            if loaded.contains(&import) {
                continue;
            }
            let id = import.parse::<OntologyId>().map_err(|_| {
                vec![diagnostic(
                    "gol::module_identity",
                    "Invalid imported module identity",
                )]
            })?;
            let owner = self
                .gol
                .as_ref()
                .and_then(|config| config.authority.module_owners.get(&id))
                .map_or_else(|| import.clone(), ToString::to_string);
            if !approved_owners.contains(&owner) {
                required_owners.insert(owner);
                continue;
            }
            if dependencies.contains_key(&owner) {
                return Err(vec![diagnostic(
                    "gol::dependency_identity",
                    "Published dependency does not declare the imported module",
                )]);
            }
            if dependencies.len() + required_owners.len() >= 31 {
                return Err(vec![diagnostic(
                    "gol::dependency_limit",
                    "GOL dependency package budget exceeded",
                )]);
            }
            let config = self.gol.as_ref().ok_or_else(|| {
                vec![diagnostic(
                    "gol::authority_unavailable",
                    "Approved GOL authority configuration is unavailable",
                )]
            })?;
            let dependency = async {
                let store = config.store(tenant, &owner).await?;
                let archive = store.open().await?;
                ensure!(
                    expected_pins.get(&owner).is_none_or(
                        |expected| *expected == archive.generation()
                    ),
                    "transitive GOL dependency generation changed"
                );
                let provenance = archive
                    .provenance()
                    .context("published GOL provenance unavailable")?;
                for (package, generation) in &provenance.dependencies {
                    let package = package.to_string();
                    cyclic_publication |= package == ontology;
                    if let Some(existing) =
                        expected_pins.insert(package.clone(), *generation)
                    {
                        ensure!(
                            existing == *generation,
                            "conflicting transitive GOL dependency pins"
                        );
                    }
                    if let Some(existing) = dependencies.get(&package) {
                        ensure!(
                            existing.generation == generation.to_string(),
                            "conflicting transitive GOL dependency pins"
                        );
                    }
                }
                let revision = provenance.source_revision.to_string();
                let mut snapshot =
                    self.store.load(tenant, Some(&revision)).await?;
                let mut source = snapshot
                    .state
                    .ontologies
                    .remove(&owner)
                    .context("published dependency source unavailable")?;
                source.files = self
                    .store
                    .load_gol_sources(tenant, &revision, &owner)
                    .await?;
                Ok::<_, anyhow::Error>((
                    source,
                    GolDependency {
                        ontology_id: owner.clone(),
                        generation: archive.generation().to_string(),
                        source_revision: revision,
                    },
                ))
            }
            .await
            .map_err(|_| {
                vec![diagnostic(
                    "gol::dependency_unavailable",
                    "An imported published module is unavailable",
                )]
            })?;
            let (local, imports) = source_imports(&dependency.0.files)?;
            if !local.contains(&import) {
                return Err(vec![diagnostic(
                    "gol::dependency_identity",
                    "Published dependency does not declare the imported module",
                )]);
            }
            for module in &local {
                let id = module.parse::<OntologyId>().map_err(|_| {
                    vec![diagnostic(
                        "gol::module_identity",
                        "Invalid imported module identity",
                    )]
                })?;
                if config
                    .authority
                    .module_owners
                    .get(&id)
                    .map(ToString::to_string)
                    .as_deref()
                    .unwrap_or(module.as_str()) !=
                    owner
                {
                    return Err(vec![diagnostic(
                        "gol::module_ownership",
                        "Published package does not own its declared modules",
                    )]);
                }
            }
            loaded.extend(local);
            pending.extend(
                imports.into_iter().filter(|name| !loaded.contains(name)),
            );
            let prefix = format!("dep_{:x}", Sha256::digest(owner.as_bytes()));
            files.extend(dependency.0.files.into_iter().map(|mut file| {
                file.path = format!("{prefix}/{}", file.path);
                file
            }));
            validate_files(&files).map_err(|_| {
                vec![diagnostic(
                    "gol::source_budget",
                    "Assembled GOL sources exceed the compilation budget",
                )]
            })?;
            dependencies.insert(owner, dependency.1);
        }
        Ok(CompilationSources {
            files,
            modules,
            dependencies: dependencies.into_values().collect(),
            required_owners,
            cyclic_publication,
        })
    }

    async fn prepare(
        &self,
        tenant: &str,
        ontology: &str,
        revision: &str,
        record: &GolSourceRecord,
        approval: &GolCompileApproval,
    ) -> Result<Prepared, Vec<GolDiagnostic>> {
        validate_approved_owners(&approval.approved_owners).map_err(|_| {
            vec![diagnostic(
                "gol::dependency_plan",
                "Invalid dependency approval",
            )]
        })?;
        let sources = match self
            .compilation_sources(
                tenant,
                ontology,
                record,
                &approval.approved_owners,
            )
            .await
        {
            Ok(sources) => sources,
            Err(diagnostics) => {
                self.require_compilation_plan(
                    tenant,
                    ontology,
                    revision,
                    &[],
                    approval,
                )
                .map_err(|_| {
                    vec![diagnostic(
                        "gol::dependency_plan",
                        "GOL dependency plan changed; authorize a new preview",
                    )]
                })?;
                return Err(diagnostics);
            },
        };
        if !sources.required_owners.is_empty() {
            return Err(vec![diagnostic(
                "gol::dependency_plan",
                "Imported source packages have not been admitted",
            )]);
        }
        self.require_compilation_plan(
            tenant,
            ontology,
            revision,
            &sources.dependencies,
            approval,
        )
        .map_err(|_| {
            vec![diagnostic(
                "gol::dependency_plan",
                "GOL dependency plan changed; authorize a new preview",
            )]
        })?;
        let CompilationSources {
            files,
            modules,
            dependencies,
            required_owners: _,
            cyclic_publication,
        } = sources;
        let empty = ExternalTypeRegistry::default();
        let external = self
            .gol
            .as_ref()
            .map_or(&empty, |config| config.external_types.as_ref());
        let mut semantic = Environment::new(external);
        if let Some(config) = &self.gol {
            for (name, value) in &config.authority.cedar_context {
                semantic.context.insert(name.clone(), *value).map_err(
                    |_| {
                        vec![diagnostic(
                            "gol::authority_unavailable",
                            "Invalid approved context schema",
                        )]
                    },
                )?;
            }
        }
        let project = analyze_files(&files, &semantic)?;
        let config = self.gol.as_ref().ok_or_else(|| {
            vec![diagnostic(
                "gol::authority_unavailable",
                "Approved GOL authority configuration is unavailable",
            )]
        })?;
        let environment =
            config.authority.environment(&project).map_err(|_| {
                vec![diagnostic(
                    "gol::authority_unavailable",
                    "Approved GOL authority configuration is invalid",
                )]
            })?;
        let security =
            galadril_ontology::authorization::compile(&project, &environment);
        let contract = security.publishable().ok_or_else(|| {
            security
                .issues()
                .iter()
                .map(|issue| diagnostic("gol::security", &issue.to_string()))
                .collect::<Vec<_>>()
        })?;
        if contract
            .flows()
            .any(|(_, flow)| !config.authority.authoring_floor.dominates(flow)) ||
            contract.routes().any(|(_, descriptor)| {
                !config
                    .authority
                    .authoring_floor
                    .dominates(descriptor.control_flow())
            })
        {
            return Err(vec![diagnostic(
                "gol::authoring_floor",
                "Approved source-authoring floor does not cover all compiled information flows",
            )]);
        }
        for (id, module) in project.modules() {
            let floor = config.authority.module_floors.get(id).map_or_else(
                || config.authority.baseline.clone(),
                |floor| config.authority.baseline.join(floor),
            );
            if !config.authority.authoring_floor.dominates(&floor) {
                return Err(vec![diagnostic(
                    "gol::authoring_floor",
                    "Approved source-authoring floor does not cover module metadata",
                )]);
            }
            for (name, declaration) in module.declarations().iter() {
                if let galadril_ontology::Declaration::IfcLabel(label) =
                    declaration
                {
                    let mut flow = galadril_ontology::authorization::Flow::new(
                        label.classification,
                    );
                    flow.compartments.clone_from(&label.compartments);
                    if let Some(transfer) = project
                        .labels()
                        .get(&module.module().reference(name.clone()))
                    {
                        flow.destinations.clone_from(&transfer.destinations);
                        flow.purposes.clone_from(&transfer.purposes);
                    }
                    if !config.authority.authoring_floor.dominates(&flow) {
                        return Err(vec![diagnostic(
                            "gol::authoring_floor",
                            "Approved source-authoring floor does not cover a declared IFC label",
                        )]);
                    }
                }
            }
        }
        let archive = compile_archive(&project, &environment)
            .map_err(archive_diagnostics)?;
        let candidates = project
            .symbols()
            .keys()
            .filter(|symbol| {
                modules.contains(&symbol.key().ontology().to_string())
            })
            .cloned()
            .collect();
        Ok(Prepared {
            archive,
            cyclic_publication,
            dependencies,
            candidates,
            modules: project
                .modules()
                .values()
                .map(|module| module.module().clone())
                .collect(),
        })
    }

    /// Captures the manifest's current source and candidate pins for a block.
    pub(crate) async fn capture_gol_binding(
        &self,
        tenant: &str,
        binding: &mut GolBindingRecord,
    ) -> Result<()> {
        validate_gol_identity(tenant, &binding.ontology_id)?;
        ensure!(
            self.store
                .load(tenant, None)
                .await?
                .state
                .ontologies
                .get(&binding.ontology_id)
                .is_some_and(|record| record.deletion_base.is_none()),
            "ontology unavailable"
        );
        let config = self
            .gol
            .as_ref()
            .context("approved GOL publication configuration unavailable")?;
        let archive = config
            .store(tenant, &binding.ontology_id)
            .await?
            .open()
            .await?;
        let candidates = archive
            .provenance()
            .context("published GOL provenance unavailable")?
            .candidates
            .iter()
            .map(symbol_key)
            .collect::<BTreeSet<_>>();
        if binding.symbols.is_empty() {
            binding.symbols = candidates.iter().cloned().collect();
        }
        ensure!(
            !binding.symbols.is_empty() &&
                binding.symbols.len() <= 4096 &&
                binding
                    .symbols
                    .iter()
                    .all(|symbol| candidates.contains(symbol)),
            "invalid GOL symbol selection"
        );
        binding.generation = archive.generation().to_string();
        binding.source_revision = archive
            .provenance()
            .context("published GOL provenance unavailable")?
            .source_revision
            .to_string();
        Ok(())
    }

    /// Checks the exact generation before releasing a pipeline that uses it.
    pub(crate) async fn check_active_gol_bindings<'a>(
        &self,
        tenant: &str,
        bindings: impl Iterator<Item = &'a GolBindingRecord>,
    ) -> Result<()> {
        let mut checked = BTreeSet::new();
        for binding in bindings {
            if !checked.insert((
                &binding.ontology_id,
                &binding.generation,
                &binding.source_revision,
            )) {
                continue;
            }
            let config = self.gol.as_ref().context(
                "approved GOL publication configuration unavailable",
            )?;
            let archive = match config
                .store(tenant, &binding.ontology_id)
                .await?
                .open_generation(binding.generation.parse()?)
                .await
            {
                Ok(archive) => archive,
                Err(error) => {
                    tracing::warn!(
                        event.name =
                            "registry.pipeline.gol_generation_unavailable"
                    );
                    return Err(error)
                        .context("bound GOL generation unavailable");
                },
            };
            ensure!(
                archive.provenance().is_some_and(|provenance| {
                    provenance.source_revision.as_ref() ==
                        binding.source_revision.as_str()
                }),
                "bound GOL source revision mismatch"
            );
        }
        Ok(())
    }

    /// Atomically stores the selected immutable generation with pipeline
    /// state.
    pub async fn put_binding(
        &self,
        tenant: &str,
        expected: &str,
        mut binding: GolBindingRecord,
    ) -> Result<(GolBindingRecord, String)> {
        validate_identity(tenant, &binding.pipeline_id)?;
        validate_identity(tenant, &binding.block_id)?;
        let mut snapshot = self.store.load(tenant, None).await?;
        ensure!(
            snapshot.revision_id == expected,
            "registry revision changed"
        );
        let pipeline = snapshot
            .state
            .pipelines
            .get(&binding.pipeline_id)
            .context("pipeline unavailable")?;
        ensure!(
            pipeline
                .definition
                .get("pipeline")
                .and_then(serde_json::Value::as_array)
                .is_some_and(|steps| steps.iter().any(|step| step
                    .get("step")
                    .and_then(serde_json::Value::as_str) ==
                    Some(&binding.block_id))),
            "pipeline block unavailable"
        );
        self.capture_gol_binding(tenant, &mut binding).await?;
        binding.updated_at_ms = Utc::now().timestamp_millis();
        snapshot.state.bindings.insert(
            TenantState::binding_key(&binding.pipeline_id, &binding.block_id),
            binding.clone(),
        );
        let revision = self
            .store
            .commit(
                tenant,
                expected,
                &snapshot.state,
                "registry",
                "Bind GOL generation",
            )
            .await?;
        Ok((binding, revision))
    }

    /// Lists immutable generation selections without disclosing ontology IR.
    pub async fn list_bindings(
        &self,
        tenant: &str,
        pipeline: Option<&str>,
        revision: Option<&str>,
        limit: usize,
    ) -> Result<Vec<GolBindingRecord>> {
        ensure!(crate::storage::valid_tenant(tenant), "invalid tenant ID");
        let snapshot = self.store.load(tenant, revision).await?;
        Ok(snapshot
            .state
            .bindings
            .into_values()
            .filter(|binding| {
                pipeline.is_none_or(|id| binding.pipeline_id == id)
            })
            .take(limit.min(100))
            .collect())
    }

    /// Resolves a deployed root's compiled block to its owning revision.
    pub async fn gol_binding(
        &self,
        tenant: &str,
        pipeline: &str,
        revision: &str,
        block: &str,
    ) -> Result<GolBindingRecord> {
        let compiled =
            self.runtime_pipeline(tenant, pipeline, revision).await?;
        let step = compiled
            .definition
            .get("pipeline")
            .and_then(Value::as_array)
            .and_then(|steps| {
                steps.iter().find(|step| {
                    step.get("step").and_then(Value::as_str) == Some(block)
                })
            })
            .context("GOL runtime block unavailable")?;
        let (pipeline, revision, block) = match step.get("origin") {
            None | Some(Value::Null) => (pipeline, revision, block),
            Some(origin) => {
                let part = |key| {
                    origin
                        .get(key)
                        .and_then(Value::as_str)
                        .context("GOL block origin unavailable")
                };
                (
                    part("pipeline_id")?,
                    part("revision_id")?,
                    part("block_id")?,
                )
            },
        };
        let mut snapshot = self.store.load(tenant, Some(revision)).await?;
        snapshot
            .state
            .bindings
            .remove(&TenantState::binding_key(pipeline, block))
            .context("GOL binding unavailable")
    }
}

/// Formats the logical selection syntax used by source and runtime contracts.
#[must_use]
pub fn symbol_key(symbol: &SymbolRef) -> String {
    format!("{}::{}", symbol.key().ontology(), symbol.key().name())
}

fn validate_gol_identity(tenant: &str, ontology: &str) -> Result<()> {
    validate_identity(tenant, ontology)?;
    ontology
        .parse::<OntologyId>()
        .context("invalid logical ontology identity")?;
    Ok(())
}

fn diagnostic(code: &str, message: &str) -> GolDiagnostic {
    GolDiagnostic {
        code: code.into(),
        message: message.into(),
        path: String::new(),
        start: 0,
        end: 0,
        severity: "error".into(),
    }
}

fn archive_diagnostics(error: ArchiveError) -> Vec<GolDiagnostic> {
    match error {
        ArchiveError::Security(issues) => issues
            .into_iter()
            .map(|issue| diagnostic("gol::security", &issue.to_string()))
            .collect(),
        _ => vec![diagnostic(
            "gol::archive",
            "Validated GOL artifact could not be compiled",
        )],
    }
}

fn source_imports(
    files: &[GolFile],
) -> Result<(BTreeSet<String>, BTreeSet<String>), Vec<GolDiagnostic>> {
    let mut modules = BTreeSet::new();
    let mut imports = BTreeSet::new();
    for file in files {
        let tree = parse_source(&file.path, &file.text).map_err(|error| {
            vec![GolDiagnostic {
                code: error.code().as_str().into(),
                message: error.to_string(),
                path: file.path.clone(),
                start: error.span().start,
                end: error.span().end,
                severity: "error".into(),
            }]
        })?;
        for module in tree.modules {
            modules.insert(
                module
                    .value
                    .name
                    .value
                    .segments
                    .iter()
                    .map(|segment| segment.value)
                    .collect::<Vec<_>>()
                    .join("."),
            );
            for declaration in module.value.declarations {
                if let Declaration::Import(import) = declaration.value &&
                    import.kind == ImportKind::Module
                {
                    imports.insert(
                        import
                            .module
                            .value
                            .segments
                            .iter()
                            .map(|segment| segment.value)
                            .collect::<Vec<_>>()
                            .join("."),
                    );
                }
            }
        }
    }
    Ok((modules, imports))
}

fn validate_approved_owners(owners: &[String]) -> Result<()> {
    ensure!(
        owners.len() <= 31,
        "GOL dependency approval budget exceeded"
    );
    let mut unique = BTreeSet::new();
    for owner in owners {
        owner.parse::<OntologyId>()?;
        ensure!(unique.insert(owner), "duplicate GOL dependency approval");
    }
    Ok(())
}

/// Separate stores cannot atomically replace a cycle of package generations.
#[derive(Debug)]
pub(crate) struct PublicationCycle;

impl std::fmt::Display for PublicationCycle {
    fn fmt(
        &self,
        formatter: &mut std::fmt::Formatter<'_>,
    ) -> std::fmt::Result {
        formatter.write_str("gol::publication_cycle: cyclic publication dependencies require one source package")
    }
}

impl std::error::Error for PublicationCycle {}
