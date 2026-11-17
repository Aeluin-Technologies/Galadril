//! Admit GOL disclosures through the compiled contract and live authorities.

mod admission;
mod effect_scope;
mod host;
mod receipts;
pub(crate) mod runtime;
mod snapshot;
mod spicedb;

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use galadril_ontology::archive::remote::{
    Generation, RemoteArchive, RemoteOptions, RemoteStore,
};
use galadril_ontology::authorization::{
    Access, Clearance, Computation, Flow, Instance, Operation, Route, SinkKind,
};
use galadril_ontology::export::{
    ExportOptions, ExportRequest, IntegerEncoding, SchemaPart, SchemaProfile,
    SymbolDescription,
};
use galadril_ontology::{
    FieldKind, Name, QualifiedName, Reference, SymbolKind, SymbolRef, TypeRef,
};
use galadril_registry::deployment_file::DeploymentFile;
use galadril_registry::gol::{GolAuthorityConfig, registered_json_mappings};
use galadril_registry::grpc::RegistryClient;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::authorization::{Authorization, Permission, QueryContext};
use super::identity::IdentityService;
use crate::application::ports::control_plane_store::{
    GolDisclosure, GolSummary,
};
use crate::application::ports::iam_store::IamStore;

/// Only admitted properties reach GraphQL or an agent.
#[derive(Debug, Serialize)]
pub struct GolProperty {
    pub key: String,
    pub name: String,
    pub type_name: String,
}

/// The transport carries a projection, never another ontology definition.
#[derive(Debug, Serialize)]
pub struct GolSymbol {
    pub key: String,
    pub name: String,
    pub kind: String,
    pub references: Vec<String>,
    pub properties: Vec<GolProperty>,
}

#[derive(Debug, Serialize)]
pub struct GolSymbols {
    pub generation: String,
    pub symbols: Vec<GolSymbol>,
}

/// Compatibility and field selections are explicit at every schema request.
pub struct SchemaQuery {
    pub ontology_id: String,
    pub generation: Option<String>,
    pub target: String,
    pub profile: SchemaProfile,
    pub fields: Vec<String>,
}

/// Trusted deployment facts are not accepted from sources or API bodies.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeProfile {
    issuer: String,
    metadata_destination: QualifiedName,
    metadata_purpose: QualifiedName,
    source_destination: QualifiedName,
    source_purpose: QualifiedName,
    context_floor: Flow,
    shared_state_floor: Flow,
    ingress_floors: BTreeMap<String, Flow>,
}

struct Backend {
    authority: GolAuthorityConfig,
    profile: RuntimeProfile,
    client: aws_sdk_s3::Client,
    bucket: String,
    raw_bucket: String,
    worker_key: Option<ring::hmac::Key>,
    signer: Option<receipts::ReceiptSigner>,
    provider: spicedb::SpiceDb,
    stores: RwLock<BTreeMap<String, BTreeMap<String, RemoteStore>>>,
}

/// Runtime reads contain no parser, import loading or source compilation.
pub struct GolService {
    registry: RegistryClient,
    identity: Arc<IdentityService>,
    iam: Arc<dyn IamStore>,
    auth: Arc<dyn Authorization>,
    backend: Option<Backend>,
    admissions: admission::AdmissionRegistry,
    workers: host::Workers,
}

impl GolService {
    /// Loads only deployment-owned configuration; absent configuration closes
    /// GOL routes without falling back to the removed ontology implementation.
    pub async fn from_environment(
        registry: RegistryClient,
        identity: Arc<IdentityService>,
        iam: Arc<dyn IamStore>,
        auth: Arc<dyn Authorization>,
        s3: Option<&crate::config::S3Config>,
        spicedb_endpoint: &str,
        spicedb_token: &str,
    ) -> Result<Self> {
        let authority = std::env::var_os("GOL_AUTHORITY_CONFIG_PATH");
        let profile = std::env::var_os("GOL_RUNTIME_CONFIG_PATH");
        let bucket = std::env::var("GOL_ARTIFACT_BUCKET").ok();
        let backend = match (authority, profile, bucket) {
            (None, None, None) => None,
            (Some(authority), Some(profile), Some(bucket)) => {
                let authority: GolAuthorityConfig = read_config(
                    DeploymentFile::Authority,
                    Path::new(&authority),
                )?;
                let profile: RuntimeProfile =
                    read_config(DeploymentFile::Runtime, Path::new(&profile))?;
                ensure!(
                    !profile.issuer.is_empty() && !bucket.is_empty(),
                    "Incomplete GOL runtime configuration"
                );
                let storage =
                    s3.context("GOL storage configuration is required")?;
                let credentials = aws_sdk_s3::config::Credentials::new(
                    &storage.access_key,
                    &storage.secret_key,
                    None,
                    None,
                    "gol-runtime",
                );
                let shared = aws_config::defaults(
                    aws_config::BehaviorVersion::latest(),
                )
                .endpoint_url(&storage.endpoint)
                .region(aws_config::Region::new(storage.region.clone()))
                .credentials_provider(credentials)
                .load()
                .await;
                let client = aws_sdk_s3::Client::from_conf(
                    aws_sdk_s3::config::Builder::from(&shared)
                        .force_path_style(true)
                        .build(),
                );
                let provider = spicedb::SpiceDb::connect(
                    spicedb_endpoint,
                    spicedb_token,
                    &authority,
                )
                .await?;
                Some(Backend {
                    authority,
                    profile,
                    client,
                    bucket,
                    raw_bucket: storage.bucket.clone(),
                    worker_key: deployment_key(
                        "GOL_WORKER_KEY_PATH",
                        DeploymentFile::WorkerKey,
                    )?
                    .map(|key| {
                        ring::hmac::Key::new(ring::hmac::HMAC_SHA256, &key)
                    }),
                    signer: deployment_key(
                        "GOL_EFFECT_KEY_PATH",
                        DeploymentFile::EffectKey,
                    )?
                    .map(|key| receipts::ReceiptSigner::new(&key))
                    .transpose()?,
                    provider,
                    stores: RwLock::new(BTreeMap::new()),
                })
            },
            _ => anyhow::bail!(
                "GOL authority, runtime profile and artifact bucket must be configured together"
            ),
        };
        Ok(Self {
            registry,
            identity,
            iam,
            auth,
            backend,
            admissions: admission::AdmissionRegistry::new(
                1024,
                Duration::from_secs(900),
            )?,
            workers: host::Workers::new()?,
        })
    }

    /// Rechecks every metadata candidate even when its index pages are cached.
    pub async fn symbols(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        ontology: &str,
        generation: Option<&str>,
    ) -> Result<GolSymbols> {
        self.ontology_access(tenant, user, context, ontology)
            .await?;
        let backend = self.backend()?;
        let archive = backend.open(tenant, ontology, generation).await?;
        let actor = self.actor(tenant, user).await?;
        let state = Arc::new(
            galadril_ontology::authorization::ExecutionContext::from_inputs(
                &actor.principal(),
                actor.clearance.clone(),
                backend.profile.context_floor.clone(),
            )?,
        );
        let mut symbols = Vec::new();
        for candidate in &archive
            .provenance()
            .context("GOL provenance unavailable")?
            .candidates
        {
            let access =
                backend.metadata_access(tenant, candidate.clone().into())?;
            let prepared = snapshot::Snapshot::prepare(
                &archive,
                &access,
                &actor,
                Arc::clone(&state),
                context,
                &backend.authority,
                &backend.profile.context_floor,
            )
            .await;
            let Ok(prepared) = prepared else {
                continue;
            };
            let projection = prepared.projection(access, BTreeSet::new())?;
            if let Some(description) = archive
                .describe(&projection, &prepared, &backend.provider)
                .await
            {
                symbols.push(symbol(description)?);
            }
        }
        Ok(GolSymbols {
            generation: archive.generation().to_string(),
            symbols,
        })
    }

    /// Exports one generation without relaunching a compiler or loading
    /// sources.
    pub async fn schema(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        query: SchemaQuery,
    ) -> Result<Value> {
        ensure!(query.fields.len() <= 1024, "GOL projection unavailable");
        self.ontology_access(tenant, user, context, &query.ontology_id)
            .await?;
        let backend = self.backend()?;
        let archive = backend
            .open(tenant, &query.ontology_id, query.generation.as_deref())
            .await?;
        let target = resolve(&archive, &query.target)?;
        let access = backend.metadata_access(tenant, target)?;
        let actor = self.actor(tenant, user).await?;
        let state = Arc::new(
            galadril_ontology::authorization::ExecutionContext::from_inputs(
                &actor.principal(),
                actor.clearance.clone(),
                backend.profile.context_floor.clone(),
            )?,
        );
        let prepared = snapshot::Snapshot::prepare(
            &archive,
            &access,
            &actor,
            state,
            context,
            &backend.authority,
            &backend.profile.context_floor,
        )
        .await?;
        let fields = query
            .fields
            .iter()
            .map(|name| name.parse())
            .collect::<Result<BTreeSet<Name>, _>>()?;
        let projection = prepared.projection(access, fields)?;
        let options = ExportOptions {
            profile: query.profile,
            integers: IntegerEncoding::DecimalStrings,
            ..ExportOptions::default()
        };
        let mappings = registered_json_mappings()?;
        let description = archive
            .describe(&projection, &prepared, &backend.provider)
            .await
            .context("GOL projection unavailable")?;
        let parts: &[SchemaPart] = if description.kind == SymbolKind::Action {
            &[SchemaPart::ActionInput, SchemaPart::ActionOutput]
        } else {
            &[SchemaPart::Value]
        };
        let mut documents = Vec::with_capacity(parts.len());
        for part in parts {
            let schema = archive
                .export_schema(
                    &ExportRequest {
                        projection: &projection,
                        part: *part,
                        options: &options,
                        mappings: &mappings,
                    },
                    &prepared,
                    &backend.provider,
                )
                .await?;
            documents.push(schema.schema().clone());
        }
        match documents.as_slice() {
            [value] => Ok(value.clone()),
            [input, output] => Ok(
                serde_json::json!({"inputSchema":input,"outputSchema":output}),
            ),
            _ => anyhow::bail!("GOL projection unavailable"),
        }
    }

    fn backend(&self) -> Result<&Backend> {
        self.backend.as_ref().context("GOL runtime unavailable")
    }

    async fn actor(
        &self,
        tenant: &str,
        user: &str,
    ) -> Result<admission::Actor> {
        self.identity.verify_user(tenant, user).await?;
        let backend = self.backend()?;
        let clearance = self
            .iam
            .gol_clearance(tenant, user)
            .await?
            .context("GOL clearance unavailable")?;
        Ok(admission::Actor {
            issuer: backend.profile.issuer.clone().into_boxed_str(),
            tenant: tenant.into(),
            subject_type: "user".into(),
            subject_id: user.into(),
            cedar: cedar_policy::EntityUid::from_type_name_and_id(
                backend.authority.cedar_principal.parse()?,
                cedar_policy::EntityId::new(user),
            ),
            clearance,
        })
    }

    async fn ontology_access(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        ontology: &str,
    ) -> Result<()> {
        let _: galadril_ontology::OntologyId = ontology.parse()?;
        self.identity.verify_user(tenant, user).await?;
        ensure!(
            self.auth
                .is_authorized(
                    user,
                    tenant,
                    Permission::View,
                    "ontology",
                    ontology,
                    Some(context)
                )
                .await?,
            "GOL unavailable"
        );
        Ok(())
    }
}

impl Backend {
    fn source_floor(&self, clearance: Clearance) -> Result<()> {
        let sink = self
            .authority
            .sinks
            .get(&self.profile.source_destination)
            .context("GOL source sink unavailable")?;
        ensure!(
            sink.kind == SinkKind::Internal &&
                sink.purposes.contains(&self.profile.source_purpose),
            "GOL source sink unavailable"
        );
        let mut destination = Flow::new(sink.clearance);
        destination.compartments = sink.compartments.clone();
        let computation = Computation::from_inputs(
            clearance,
            self.authority
                .authoring_floor
                .join(&self.profile.context_floor),
        )?;
        computation.check_write(
            &destination,
            &self.profile.source_destination,
            &self.profile.source_purpose,
        )?;
        Ok(())
    }

    async fn open(
        &self,
        tenant: &str,
        ontology: &str,
        generation: Option<&str>,
    ) -> Result<RemoteArchive> {
        let cached = self
            .stores
            .read()
            .map_err(|_| anyhow::anyhow!("GOL cache unavailable"))?
            .get(tenant)
            .and_then(|items| items.get(ontology))
            .cloned();
        let store = if let Some(store) = cached {
            store
        } else {
            let namespace =
                format!("t_{:x}", Sha256::digest(tenant.as_bytes()))
                    .parse()?;
            let store = RemoteStore::connect_namespace(
                self.client.clone(),
                &self.bucket,
                tenant,
                namespace,
                ontology.parse()?,
                RemoteOptions::default(),
            )
            .await?;
            let mut stores = self
                .stores
                .write()
                .map_err(|_| anyhow::anyhow!("GOL cache unavailable"))?;
            ensure!(
                stores.values().map(BTreeMap::len).sum::<usize>() < 1024,
                "GOL runtime capacity reached"
            );
            stores
                .entry(tenant.to_owned())
                .or_default()
                .insert(ontology.to_owned(), store.clone());
            store
        };
        let archive = match generation {
            Some(id) => {
                store.open_generation(id.parse::<Generation>()?).await?
            },
            None => store.open().await?,
        };
        let versions = archive.versions();
        let provenance =
            archive.provenance().context("GOL provenance unavailable")?;
        ensure!(
            versions.contract == self.authority.revision &&
                versions.relationship_schema ==
                    self.authority.relationship_schema &&
                versions.bridge == self.authority.bridge &&
                versions.cedar == self.authority.cedar_revision &&
                versions.ifc == galadril_ontology::Version::new(2, 0, 0) &&
                versions.ontologies == provenance.modules,
            "GOL authority changed"
        );
        Ok(archive)
    }

    fn metadata_access(
        &self,
        tenant: &str,
        target: Reference,
    ) -> Result<Access> {
        Ok(Access {
            instance: Instance {
                tenant: tenant.into(),
                owner: owner(&target)?.clone(),
                id: "__schema".into(),
            },
            route: Route::new(target, Operation::Describe),
            destination: self.profile.metadata_destination.clone(),
            purpose: self.profile.metadata_purpose.clone(),
        })
    }
}

#[async_trait::async_trait]
impl GolDisclosure for GolService {
    async fn admit_creation(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        _ontology: &str,
    ) -> Result<()> {
        let actor = self.actor(tenant, user).await?;
        ensure!(
            self.auth
                .is_authorized(
                    user,
                    tenant,
                    Permission::CreateOntology,
                    "tenant",
                    tenant,
                    Some(context)
                )
                .await?,
            "GOL source access denied"
        );
        self.backend()?.source_floor(actor.clearance)
    }

    async fn admit_catalog(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        summary: &GolSummary,
    ) -> Result<bool> {
        if self
            .admit_source(
                tenant,
                user,
                context,
                &summary.ontology_id,
                Some(&summary.source_revision),
            )
            .await
            .is_ok()
        {
            return Ok(true);
        }
        if summary.active_generation.is_none() {
            return Ok(false);
        }
        match self
            .symbols(
                tenant,
                user,
                context,
                &summary.ontology_id,
                summary.active_generation.as_deref(),
            )
            .await
        {
            Ok(result) => Ok(!result.symbols.is_empty()),
            Err(_) => Ok(false),
        }
    }

    async fn admit_source(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        ontology: &str,
        _revision: Option<&str>,
    ) -> Result<()> {
        let backend = self.backend()?;
        let actor = self.actor(tenant, user).await?;
        let exists = self
            .auth
            .is_authorized(
                user,
                tenant,
                Permission::Manage,
                "ontology",
                ontology,
                Some(context),
            )
            .await?;
        ensure!(exists, "GOL source access denied");
        backend.source_floor(actor.clearance)
    }
}

fn read_config<T: serde::de::DeserializeOwned>(
    kind: DeploymentFile,
    path: &Path,
) -> Result<T> {
    let bytes = kind
        .read(path)
        .context("GOL deployment configuration unavailable")?;
    serde_json::from_slice(&bytes)
        .context("Invalid GOL deployment configuration")
}

fn owner(reference: &Reference) -> Result<&SymbolRef> {
    match reference {
        Reference::Symbol(symbol) => Ok(symbol),
        Reference::Field(field) => Ok(&field.owner),
        _ => anyhow::bail!("GOL target unavailable"),
    }
}

fn resolve(archive: &RemoteArchive, key: &str) -> Result<Reference> {
    ensure!(key.len() <= 512, "GOL target unavailable");
    let provenance =
        archive.provenance().context("GOL provenance unavailable")?;
    for candidate in &provenance.candidates {
        let prefix = format!(
            "{}::{}",
            candidate.key().ontology(),
            candidate.key().name()
        );
        if key == prefix {
            return Ok(candidate.clone().into());
        }
        if let Some(field) = key
            .strip_prefix(&prefix)
            .and_then(|tail| tail.strip_prefix('.'))
        {
            if !field.contains('.') {
                return Ok(galadril_ontology::FieldRef::new(
                    candidate.clone(),
                    field.parse()?,
                )
                .into());
            }
        }
    }
    anyhow::bail!("GOL target unavailable")
}

fn symbol(value: SymbolDescription) -> Result<GolSymbol> {
    let owner = owner(&value.target)?;
    let key = match &value.target {
        Reference::Field(field) => format!(
            "{}::{}.{}",
            owner.key().ontology(),
            owner.key().name(),
            field.name
        ),
        _ => format!("{}::{}", owner.key().ontology(), owner.key().name()),
    };
    let mut references = BTreeSet::new();
    let mut properties = Vec::with_capacity(value.properties.len());
    for property in value.properties {
        let ty = match property.kind {
            FieldKind::Key(ty) | FieldKind::Value(ty) => ty,
            FieldKind::OwnedAttribute { attribute, .. } => {
                TypeRef::Named(attribute)
            },
            _ => anyhow::bail!("GOL property unavailable"),
        };
        let type_name = type_name(&ty, &mut references)?;
        properties.push(GolProperty {
            key: format!("{key}.{}", property.name),
            name: property.name.to_string(),
            type_name,
        });
    }
    for role in value.roles {
        references.insert(format!(
            "{}::{}",
            role.role.entity.key().ontology(),
            role.role.entity.key().name()
        ));
    }
    Ok(GolSymbol {
        key,
        name: owner.key().name().to_string(),
        kind: kind(value.kind)?.to_owned(),
        references: references.into_iter().collect(),
        properties,
    })
}

fn kind(kind: SymbolKind) -> Result<&'static str> {
    Ok(match kind {
        SymbolKind::Entity => "entity",
        SymbolKind::Relation => "relation",
        SymbolKind::Attribute => "attribute",
        SymbolKind::TypeAlias => "type",
        SymbolKind::Struct => "struct",
        SymbolKind::Enum => "enum",
        SymbolKind::Invariant => "invariant",
        SymbolKind::Derive => "derive",
        SymbolKind::View => "view",
        SymbolKind::Action => "action",
        _ => anyhow::bail!("GOL category unavailable"),
    })
}

fn type_name(
    ty: &TypeRef,
    references: &mut BTreeSet<String>,
) -> Result<String> {
    Ok(match ty {
        TypeRef::Primitive(primitive) => format!("{primitive:?}"),
        TypeRef::Named(symbol) | TypeRef::Entity(symbol) => {
            let key = format!(
                "{}::{}",
                symbol.key().ontology(),
                symbol.key().name()
            );
            references.insert(key.clone());
            if matches!(ty, TypeRef::Entity(_)) {
                format!("Ref<{key}>")
            } else {
                key
            }
        },
        TypeRef::Option(inner) => {
            format!("Option<{}>", type_name(inner, references)?)
        },
        TypeRef::List { element, .. } => {
            format!("List<{}>", type_name(element, references)?)
        },
        TypeRef::External(id) => id.name().to_string(),
        _ => anyhow::bail!("GOL type unavailable"),
    })
}

/// Read only deployment-owned exact-size keys; missing keys disable worker
/// authority.
fn deployment_key(
    name: &str,
    kind: DeploymentFile,
) -> Result<Option<[u8; 32]>> {
    std::env::var_os(name)
        .map(|path| kind.read_key(Path::new(&path)))
        .transpose()
        .context("GOL deployment credential unavailable")
}
