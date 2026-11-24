//! Private S3 snapshots and atomic publication of GOLC generations.

mod cache;
mod publication;
mod s3;

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::Arc;
use std::time::{Duration, SystemTime};
use std::{fmt, io};

use aws_sdk_s3::Client;
use cache::{Cache, Counters};
pub use publication::Cleanup;
#[cfg(feature = "parser")]
pub use publication::Staged;
use s3::{S3, Source};
use serde::{Deserialize, Serialize};
use tokio::time::Instant;
use uuid::Uuid;

use super::{
    ArchivePin, ArchiveReader, Direction, InvariantContractError, Symbol,
};
use crate::authorization::{Access, PermissionProvider, TrustedRuntime};
use crate::{ModuleVersion, Name, OntologyId, QualifiedName, SymbolRef};

/// Storage and resource bounds shared by the trusted backend.
#[derive(Clone, Debug)]
pub struct RemoteOptions {
    /// Maximum lifetime of a generation held by a reader.
    pub reader_window: Duration,
    /// Maximum total staging and activation lifetime, including upload.
    pub publication_window: Duration,
    /// Deadline for one storage operation, including its response body.
    pub timeout: Duration,
    /// Maximum aggregate size of the memory cache.
    pub ram_bytes: usize,
    /// Optional backend-only directory for anonymous SSD cache files.
    pub disk: Option<DiskCache>,
    /// Maximum number of independent index paths per batch (1–8).
    pub parallelism: usize,
}

impl Default for RemoteOptions {
    fn default() -> Self {
        Self {
            reader_window: Duration::from_secs(30),
            publication_window: Duration::from_secs(300),
            timeout: Duration::from_secs(5),
            ram_bytes: 16 * 1024 * 1024,
            disk: None,
            parallelism: 1,
        }
    }
}

impl RemoteOptions {
    /// Validates the timing, space, and concurrency limits before connecting.
    pub fn validate(&self) -> Result<(), RemoteError> {
        let valid_window = |window: Duration| {
            (Duration::from_millis(1)..=Duration::from_secs(3_600))
                .contains(&window) &&
                window == Duration::from_millis(window.as_millis() as u64)
        };
        if !valid_window(self.reader_window) ||
            !valid_window(self.publication_window) ||
            !valid_window(self.timeout) ||
            !matches!(self.parallelism, 1..=8) ||
            self.disk.as_ref().is_some_and(|disk| disk.bytes == 0)
        {
            return Err(RemoteError::Configuration);
        }
        Ok(())
    }
}

/// Space bound for anonymous temporary cache files.
#[derive(Clone, Debug)]
pub struct DiskCache {
    /// Local SSD directory accessible only to the backend.
    pub directory: PathBuf,
    /// Maximum aggregate content size.
    pub bytes: usize,
}

/// A globally unique generation within the trusted storage namespace.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize,
)]
#[serde(transparent)]
pub struct Generation(Uuid);

impl Generation {
    fn new() -> Self {
        Self(Uuid::new_v4())
    }
}

impl fmt::Display for Generation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.simple().fmt(f)
    }
}

impl FromStr for Generation {
    type Err = RemoteError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        if value.len() != 32 ||
            !value.bytes().all(|byte| {
                byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')
            })
        {
            return Err(RemoteError::Configuration);
        }
        Uuid::parse_str(value)
            .map(Self)
            .map_err(|_| RemoteError::Configuration)
    }
}

/// Private compilation provenance activated with the matching artifact.
///
/// Candidate names are inputs to authorization, never a public discovery
/// result. Only the gate's authorized descriptions may leave the backend.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicationProvenance {
    /// Immutable source commit selected by the publishing Registry.
    pub source_revision: Box<str>,
    /// Exact imported generations used by the compiler.
    pub dependencies: BTreeMap<OntologyId, Generation>,
    /// Logical candidate symbols belonging to the published source package.
    pub candidates: Vec<SymbolRef>,
    /// Exact module versions contained in the archive.
    pub modules: BTreeSet<ModuleVersion>,
}

impl PublicationProvenance {
    fn valid(&self) -> bool {
        !self.source_revision.is_empty() &&
            self.source_revision.len() <= 256 &&
            self.source_revision.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
            }) &&
            self.dependencies.len() <= 256 &&
            self.modules.len() <= 256 &&
            self.candidates.len() <= 4096 &&
            self.candidates.iter().all(|symbol| {
                self.modules.iter().any(|module| {
                    module.ontology() == symbol.key().ontology() &&
                        module.version() == symbol.version()
                })
            }) &&
            self.candidates.windows(2).all(|pair| match pair {
                [left, right] => left < right,
                _ => false,
            })
    }
}

/// Backend-only storage failure categories. Query methods collapse errors
/// to the same unavailable result as a denied disclosure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum RemoteError {
    /// Local bounds or the manifest timing profile are incompatible.
    Configuration,
    /// The bucket has versioning or its conditional writes failed the probe.
    UnsupportedStorage,
    /// The expected active generation changed.
    Conflict,
    /// The generation's read or publication window has expired.
    Expired,
    /// The required manifest or artifact is absent.
    Missing,
    /// The network request or its streamed response exceeded its deadline.
    Timeout,
    /// The storage service could not complete the operation.
    Unavailable,
    /// Returned bytes, metadata, or the archive root failed validation.
    Integrity,
    /// Cleanup has yet to observe the complete retirement interval.
    Retained,
    /// Too many outstanding publications require cleanup.
    Capacity,
}

/// Private failure of a remote metadata projection by a trusted host.
#[cfg(feature = "export")]
#[derive(Debug)]
pub enum RemoteDescriptionError {
    /// The reader's storage or lifetime prevented the projection.
    Archive(RemoteError),
    /// The checked projection was denied or could not be completed.
    Projection(crate::export::ExportError),
}

impl fmt::Display for RemoteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "GOLC remote operation: {self:?}")
    }
}

impl RemoteError {
    fn io(self) -> io::Error {
        let kind = match self {
            Self::Timeout | Self::Expired => io::ErrorKind::TimedOut,
            Self::Missing => io::ErrorKind::NotFound,
            Self::Integrity => io::ErrorKind::InvalidData,
            _ => io::ErrorKind::Other,
        };
        io::Error::new(kind, self)
    }
}

impl std::error::Error for RemoteError {}

/// Metrics from exact archive GETs and the verified content caches.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReadMetrics {
    /// Range GET operations started by the adapter, excluding SDK retries.
    pub requests: u64,
    /// Successful response body bytes, excluding HTTP headers.
    pub bytes: u64,
    /// Manifest GET operations started by the adapter.
    pub manifest_requests: u64,
    /// Successfully loaded manifest bytes, excluding HTTP headers.
    pub manifest_bytes: u64,
    /// Fragment hits in process memory.
    pub ram_hits: u64,
    /// Fragment hits on the local SSD.
    pub disk_hits: u64,
    /// Requested fragments absent from both caches.
    pub misses: u64,
}

/// A private, tenant-scoped RustFS/S3 namespace owned by the backend.
///
/// # Examples
///
/// The service selects its namespace and retains storage credentials. The
/// returned snapshot is bound to the active manifest's generation.
///
/// ```no_run
/// use galadril_ontology::archive::remote::{
///     Generation, RemoteOptions, RemoteStore,
/// };
///
/// # async fn example(client: aws_sdk_s3::Client)
/// #     -> Result<Generation, Box<dyn std::error::Error>> {
/// let store = RemoteStore::connect(
///     client,
///     "ontology-artifacts",
///     "acme".parse()?,
///     "communication.mail".parse()?,
///     RemoteOptions::default(),
/// )
/// .await?;
/// let snapshot = store.open().await?;
/// # Ok(snapshot.generation())
/// # }
/// ```
#[derive(Clone)]
pub struct RemoteStore {
    storage: S3,
    tenant: Box<str>,
    options: RemoteOptions,
    cache: Arc<Cache>,
    counters: Arc<Counters>,
}

impl RemoteStore {
    /// Validates an unversioned bucket and probes conditional writes using
    /// isolated temporary keys before enabling publication or cleanup.
    pub async fn connect(
        client: Client,
        bucket: &str,
        tenant: Name,
        project: QualifiedName,
        options: RemoteOptions,
    ) -> Result<Self, RemoteError> {
        Self::connect_namespace(
            client,
            bucket,
            tenant.as_str(),
            tenant.clone(),
            project,
            options,
        )
        .await
    }

    /// Separates authenticated tenant identity from its safe storage segment.
    ///
    /// The host owns namespace selection. Hashing an IAM tenant must never
    /// rewrite the identity checked by the archive's authorization boundary.
    pub async fn connect_namespace(
        client: Client,
        bucket: &str,
        logical_tenant: &str,
        namespace: Name,
        project: QualifiedName,
        options: RemoteOptions,
    ) -> Result<Self, RemoteError> {
        options.validate()?;
        if bucket.is_empty() ||
            logical_tenant.is_empty() ||
            logical_tenant.len() > 64 ||
            !logical_tenant.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-')
            })
        {
            return Err(RemoteError::Configuration);
        }
        let prefix = format!("gol/v1/{namespace}/{project}/");
        let storage = S3::new(client, bucket, prefix, options.timeout);
        storage.ensure_unversioned().await?;
        storage.probe().await?;
        let cache =
            Arc::new(Cache::new(options.ram_bytes, options.disk.clone()));
        Ok(Self {
            storage,
            tenant: logical_tenant.into(),
            options,
            cache,
            counters: Arc::default(),
        })
    }

    /// Reads the current generation for use as an expected publication value.
    pub async fn active(&self) -> Result<Option<Generation>, RemoteError> {
        Ok(self.load().await?.state.active.map(|artifact| artifact.id))
    }

    /// Opens the current archive under a single bounded generation snapshot.
    pub async fn open(&self) -> Result<RemoteArchive, RemoteError> {
        self.open_selected(None).await
    }

    /// Opens an exact active generation without renewing retired retention.
    ///
    /// Existing handles retain their original bounded lease across activation.
    /// A new request cannot prolong a retired generation's cleanup deadline.
    pub async fn open_generation(
        &self,
        generation: Generation,
    ) -> Result<RemoteArchive, RemoteError> {
        self.open_selected(Some(generation)).await
    }

    async fn open_selected(
        &self,
        expected: Option<Generation>,
    ) -> Result<RemoteArchive, RemoteError> {
        let lifetime = Window::new(self.options.reader_window);
        let open = async {
            let artifact = self
                .load()
                .await?
                .state
                .active
                .ok_or(RemoteError::Missing)?;
            let generation = artifact.id;
            if expected.is_some_and(|expected| expected != generation) {
                return Err(RemoteError::Conflict);
            }
            let provenance = artifact.provenance.clone();
            let pin = ArchivePin(artifact.pin);
            let reader = ArchiveReader::open_async(
                Source::new(self, artifact, lifetime),
                pin,
            )
            .await
            .map_err(map_archive)?;
            Ok(RemoteArchive {
                reader,
                generation,
                tenant: self.tenant.clone(),
                lifetime,
                provenance,
            })
        };
        within(lifetime, open).await
    }

    /// Returns cumulative cache and network counters for this store.
    #[must_use]
    pub fn metrics(&self) -> ReadMetrics {
        self.counters.read()
    }
}

fn map_archive(error: super::ArchiveError) -> RemoteError {
    if let super::ArchiveError::Io(error) = error {
        return match error.kind() {
            io::ErrorKind::TimedOut => RemoteError::Timeout,
            io::ErrorKind::NotFound => RemoteError::Missing,
            io::ErrorKind::InvalidData => RemoteError::Integrity,
            _ => RemoteError::Unavailable,
        };
    }
    RemoteError::Integrity
}

/// An archive handle whose generation, tenant, and use window are fixed.
pub struct RemoteArchive {
    reader: ArchiveReader<Source>,
    generation: Generation,
    tenant: Box<str>,
    lifetime: Window,
    provenance: Option<PublicationProvenance>,
}

impl RemoteArchive {
    /// Reports whether the original reader window still permits operations.
    ///
    /// Hosts may reopen an active generation after expiration. Each operation
    /// also enforces the window because this observation can become stale.
    #[must_use]
    pub fn is_live(&self) -> bool {
        self.lifetime.live()
    }

    /// Borrows artifact pins for independent verification by the trusted host.
    #[must_use]
    pub fn versions(&self) -> &crate::authorization::Versions {
        self.reader.versions()
    }

    /// Loads private verified requirements for a trusted runtime resolver.
    pub async fn requirements(
        &self,
        route: &crate::authorization::Route,
    ) -> Result<
        std::collections::BTreeMap<
            crate::authorization::Route,
            crate::authorization::Descriptor,
        >,
        RemoteError,
    > {
        within(self.lifetime, async {
            self.reader.requirements(route).await.map_err(map_archive)
        })
        .await
    }

    /// Checks current obligations without granting an effect capability.
    /// The trusted host must retain its effect boundary through actual I/O.
    pub async fn authorize(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> crate::authorization::Decision {
        use crate::authorization::{Decision, Denial};
        match self.authorize_checked(access, runtime, provider).await {
            Ok(Decision::Allow) => Decision::Allow,
            _ => Decision::Deny(Denial::Runtime),
        }
    }

    /// Returns storage errors and policy decisions for trusted host
    /// diagnostics.
    ///
    /// Do not disclose denial categories or storage errors to callers that
    /// lack access to the underlying artifact.
    pub async fn authorize_checked(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<crate::authorization::Decision, RemoteError> {
        use crate::authorization::{Decision, Denial};
        if !self.owns(access) {
            return Ok(Decision::Deny(Denial::Runtime));
        }
        within(self.lifetime, async {
            self.reader
                .authorization_decision(access, runtime, provider)
                .await
                .map_err(map_archive)
        })
        .await
    }

    /// Releases authorized metadata only within this generation's use window.
    #[cfg(feature = "export")]
    pub async fn describe(
        &self,
        projection: &crate::export::Projection,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> Option<crate::export::SymbolDescription> {
        self.describe_checked(projection, runtime, provider)
            .await
            .ok()
    }

    /// Separates an expired reader from a denied or unavailable projection.
    #[cfg(feature = "export")]
    pub async fn describe_checked(
        &self,
        projection: &crate::export::Projection,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> Result<crate::export::SymbolDescription, RemoteDescriptionError> {
        if !self.owns(&projection.root.access) ||
            !projection
                .definitions
                .values()
                .all(|selection| self.owns(&selection.access))
        {
            return Err(RemoteDescriptionError::Projection(
                crate::export::ExportError::Unavailable,
            ));
        }
        within(self.lifetime, async {
            Ok(self
                .reader
                .describe_checked(projection, runtime, provider)
                .await)
        })
        .await
        .map_err(RemoteDescriptionError::Archive)?
        .map_err(RemoteDescriptionError::Projection)
    }

    /// Admits an effect while this snapshot is live. Once admitted, storage
    /// work is not cancelled at expiry: its outcome must retain its label
    /// even when the backend has already committed a mutation.
    pub async fn perform<T, E, F: Future<Output = Result<T, E>>>(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
        effect: impl FnOnce() -> F,
    ) -> Result<
        crate::authorization::EffectReceipt<T>,
        crate::authorization::EffectError,
    > {
        use crate::authorization::{Denial, EffectError};
        if !self.owns(access) || !self.lifetime.live() {
            return Err(EffectError::Denied(Denial::Runtime));
        }
        self.reader
            .perform(access, runtime, provider, || async {
                if !self.lifetime.live() {
                    return Err(EffectError::Unavailable);
                }
                effect().await.map_err(|_| EffectError::Unavailable)
            })
            .await
    }

    /// Rechecks the receipt's tenant and live read policies before disclosure.
    pub async fn inspect<'a, T>(
        &self,
        receipt: &'a crate::authorization::EffectReceipt<T>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<
        &'a Result<T, crate::authorization::EffectError>,
        crate::authorization::EffectError,
    > {
        use crate::authorization::{Denial, EffectError};
        if receipt.tenant() != self.tenant.as_ref() {
            return Err(EffectError::Denied(Denial::Runtime));
        }
        within(self.lifetime, async {
            Ok(self.reader.inspect(receipt, runtime, provider).await)
        })
        .await
        .map_err(|_| EffectError::Unavailable)?
    }

    /// Projects an authorized schema within this generation's use window.
    #[cfg(feature = "export")]
    pub async fn export_schema(
        &self,
        request: &crate::export::ExportRequest<'_>,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> Result<crate::export::SchemaDocument, crate::export::ExportError>
    {
        if !self.owns(&request.projection.root.access) {
            return Err(crate::export::ExportError::Unavailable);
        }
        within(self.lifetime, async {
            Ok(self.reader.export_schema(request, runtime, provider).await)
        })
        .await
        .map_err(|_| crate::export::ExportError::Unavailable)?
    }

    /// Validates a candidate against fresh permissions and GOL obligations.
    #[cfg(feature = "export")]
    pub async fn validate_proposal(
        &self,
        request: &crate::export::ExportRequest<'_>,
        proposal: &serde_json::Value,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
        business: &(impl crate::export::GolValidator + Sync),
    ) -> Result<(), crate::export::ExportError> {
        if !self.owns(&request.projection.root.access) {
            return Err(crate::export::ExportError::Unavailable);
        }
        within(self.lifetime, async {
            Ok(self
                .reader
                .validate_proposal(
                    request, proposal, runtime, provider, business,
                )
                .await)
        })
        .await
        .map_err(|_| crate::export::ExportError::Unavailable)?
    }

    /// Keeps schema metadata and a concrete write instance independently
    /// gated.
    #[cfg(feature = "export")]
    pub async fn validate_proposal_for(
        &self,
        request: &crate::export::ExportRequest<'_>,
        proposal: &serde_json::Value,
        write: &Access,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
        business: &(impl crate::export::GolValidator + Sync),
    ) -> Result<(), crate::export::ExportError> {
        if !self.owns(&request.projection.root.access) || !self.owns(write) {
            return Err(crate::export::ExportError::Unavailable);
        }
        within(self.lifetime, async {
            Ok(self
                .reader
                .validate_proposal_for(
                    request, proposal, write, runtime, provider, business,
                )
                .await)
        })
        .await
        .map_err(|_| crate::export::ExportError::Unavailable)?
    }

    /// Returns the immutable generation attested by the manifest.
    #[must_use]
    pub fn generation(&self) -> Generation {
        self.generation
    }

    /// Borrows private provenance pinned by this handle's exact manifest
    /// entry.
    #[must_use]
    pub fn provenance(&self) -> Option<&PublicationProvenance> {
        self.provenance.as_ref()
    }

    /// Loads bounded invariant metadata under this snapshot's live gate.
    ///
    /// The result is for trusted validation code and must not be returned to
    /// clients. Admission and a passing invariant do not authorize a mutation.
    pub async fn invariant_contract(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<crate::validation::Contract> {
        self.invariant_contract_checked(access, runtime, provider)
            .await
            .ok()
    }

    /// Loads invariant metadata while retaining the exact policy or storage
    /// failure.
    ///
    /// Denial and archive categories are private to the trusted host.
    pub async fn invariant_contract_checked(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<crate::validation::Contract, InvariantContractError<RemoteError>>
    {
        if !self.owns(access) {
            return Err(InvariantContractError::Denied(
                crate::authorization::Denial::Runtime,
            ));
        }
        within(self.lifetime, async {
            Ok(self
                .reader
                .invariant_contract_checked(access, runtime, provider)
                .await)
        })
        .await
        .map_err(InvariantContractError::Archive)?
        .map_err(|error| match error {
            InvariantContractError::Denied(reason) => {
                InvariantContractError::Denied(reason)
            },
            InvariantContractError::Archive(error) => {
                InvariantContractError::Archive(map_archive(error))
            },
        })
    }

    /// Returns the exact record after current authorization.
    pub async fn read(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Symbol> {
        if !self.owns(access) {
            return None;
        }
        self.run(self.reader.read(access, runtime, provider)).await
    }

    /// Releases a projection only when every requested item is authorized.
    pub async fn project(
        &self,
        items: &[Access],
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Vec<Symbol>> {
        if !items.iter().all(|item| self.owns(item)) {
            return None;
        }
        self.run(self.reader.project(items, runtime, provider))
            .await
    }

    /// Returns only the authorized symbols from the supplied candidates.
    pub async fn discover(
        &self,
        candidates: &[Access],
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Vec<Symbol>> {
        if !candidates.iter().all(|item| self.owns(item)) {
            return None;
        }
        self.run(self.reader.discover(candidates, runtime, provider))
            .await
    }

    /// Checks the anchor and every returned endpoint on this generation.
    pub async fn links(
        &self,
        anchor: &Access,
        candidates: &[Access],
        direction: Direction,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Vec<Symbol>> {
        if !self.owns(anchor) || !candidates.iter().all(|item| self.owns(item))
        {
            return None;
        }
        self.run(
            self.reader
                .links(anchor, candidates, direction, runtime, provider),
        )
        .await
    }

    fn owns(&self, access: &Access) -> bool {
        access.instance.tenant.as_ref() == self.tenant.as_ref()
    }

    async fn run<T>(
        &self,
        request: impl Future<Output = Option<T>>,
    ) -> Option<T> {
        match within(self.lifetime, async { Ok(request.await) }).await {
            Ok(value) => value,
            Err(error) => {
                tracing::debug!(event.name = "gol.remote.snapshot_unavailable", reason = ?error);
                None
            },
        }
    }
}

#[derive(Clone, Copy)]
struct Window {
    deadline: Instant,
    wall_start: SystemTime,
    duration: Duration,
}

impl Window {
    fn new(duration: Duration) -> Self {
        Self {
            deadline: Instant::now() + duration,
            wall_start: SystemTime::now(),
            duration,
        }
    }

    fn live(self) -> bool {
        Instant::now() < self.deadline &&
            self.wall_start
                .elapsed()
                .is_ok_and(|elapsed| elapsed < self.duration)
    }
}

async fn within<T>(
    window: Window,
    request: impl Future<Output = Result<T, RemoteError>>,
) -> Result<T, RemoteError> {
    if !window.live() {
        return Err(RemoteError::Expired);
    }
    let result = tokio::time::timeout_at(window.deadline, request)
        .await
        .map_err(|_| RemoteError::Expired)?;
    if !window.live() {
        return Err(RemoteError::Expired);
    }
    result
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Artifact {
    id: Generation,
    pin: [u8; 32],
    bytes: u64,
    etag: Box<str>,
    provenance: Option<PublicationProvenance>,
}
