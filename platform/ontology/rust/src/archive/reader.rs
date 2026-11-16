//! Seek by range and disclose metadata only after the phase-four decision.

use std::collections::BTreeMap;
use std::fs::File;
use std::future::Future;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};

use golc_wire::{Block, DeclarationTag, Kind, ScalarTag};

use super::container::{
    AUTHORITY, DECLARATIONS, HEADER_LEN, MAX_LEVEL, PAGE_SIZE, Pointer,
    SCALARS, block, digest, header, link_key, route_key, target_key,
};
use super::{ArchiveError, ArchivePin, Direction, Symbol};
use crate::authorization::{
    Access, Decision, Denial, Descriptor, EffectError, EffectReceipt,
    Operation, PermissionProvider, Route, TrustedRuntime, archive as auth,
};

/// An exact archive range whose digest is authenticated by its parent.
#[derive(Debug, Clone, Copy)]
pub struct RangeRequest {
    /// Starting byte offset.
    pub offset: u64,
    /// Exact byte count.
    pub length: usize,
    /// Expected SHA-256.
    pub sha256: [u8; 32],
}

/// An immutable archive source with observable positional reads.
///
/// Implementations return an error when any requested byte is unavailable.
pub trait RangeReader {
    /// Returns the length of the snapshot.
    fn length(&self) -> io::Result<u64>;

    /// Fills exactly the requested range without changing reader position.
    fn read_exact_at(&self, offset: u64, buffer: &mut [u8]) -> io::Result<()>;

    /// Reads asynchronously when the backing store performs network I/O.
    fn fetch(
        &self,
        range: RangeRequest,
    ) -> impl Future<Output = io::Result<Arc<[u8]>>> + Send {
        std::future::ready(sync_fetch(self, range))
    }

    /// Returns a bound on independent index paths and records per batch.
    fn parallelism(&self) -> usize {
        1
    }

    /// Reads a batch in input order. Backends may group contiguous ranges.
    fn fetch_many<'a>(
        &'a self,
        ranges: &'a [RangeRequest],
    ) -> impl Future<Output = io::Result<Vec<Arc<[u8]>>>> + Send + 'a {
        std::future::ready(
            ranges
                .iter()
                .map(|range| sync_fetch(self, *range))
                .collect(),
        )
    }

    /// Indicates whether the archive should retain its own directory cache.
    fn cache_index(&self) -> bool {
        true
    }
}

fn sync_fetch(
    reader: &(impl RangeReader + ?Sized),
    range: RangeRequest,
) -> io::Result<Arc<[u8]>> {
    let mut buffer = vec![0; range.length];
    reader.read_exact_at(range.offset, &mut buffer)?;
    Ok(buffer.into())
}

impl RangeReader for &[u8] {
    fn length(&self) -> io::Result<u64> {
        Ok(self.len() as u64)
    }

    fn read_exact_at(&self, offset: u64, buffer: &mut [u8]) -> io::Result<()> {
        usize::try_from(offset)
            .ok()
            .and_then(|from| self.get(from..))
            .and_then(|slice| slice.get(..buffer.len()))
            .ok_or(io::ErrorKind::UnexpectedEof)?
            .read_exact(buffer)
    }
}

/// Positional reader over a local file kept open for the archive's lifetime.
pub struct LocalFile(Mutex<File>);

impl LocalFile {
    /// Opens a local artifact path supplied by the trusted host.
    pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
        File::open(path).map(|file| Self(Mutex::new(file)))
    }
}

impl RangeReader for LocalFile {
    fn length(&self) -> io::Result<u64> {
        let file = self
            .0
            .lock()
            .map_err(|_| io::Error::other("GOLC file lock"))?;
        Ok(file.metadata()?.len())
    }

    fn read_exact_at(&self, offset: u64, buffer: &mut [u8]) -> io::Result<()> {
        let mut file = self
            .0
            .lock()
            .map_err(|_| io::Error::other("GOLC file lock"))?;
        file.seek(SeekFrom::Start(offset))?;
        file.read_exact(buffer)
    }
}

/// A sparse reader pinned to an approved immutable publication.
///
/// Index pages and linked Cedar state can be reused. Every disclosure still
/// checks live permissions and current, request-bound runtime evidence.
pub struct ArchiveReader<R> {
    index: Index<R>,
    authority: auth::ArchivedAuthority,
}

impl<R: RangeReader> ArchiveReader<R> {
    /// Loads an owner's invariant contract after current read or write
    /// admission.
    ///
    /// This trusted-host API contains private dependency metadata and grants
    /// no right to apply a proposal. Missing, oversized, or corrupt
    /// contracts use the same unavailable result as denial; absence never
    /// means no invariants.
    pub async fn invariant_contract(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<crate::validation::Contract> {
        let result = async {
            if !matches!(
                access.route.operation,
                Operation::Read | Operation::Write
            ) || !self.permitted(access, runtime, provider).await?
            {
                return Ok(None);
            }
            let owner = match &access.route.target {
                crate::Reference::Symbol(owner) => owner,
                crate::Reference::Field(field) => &field.owner,
            };
            if owner != &access.instance.owner {
                return Err(ArchiveError::Invalid);
            }
            let key = format!(
                "v/{}",
                target_key(&crate::Reference::Symbol(owner.clone()))
            );
            let Some(pointer) = self.index.locate(&key).await? else {
                return Err(ArchiveError::Invalid);
            };
            let limits = crate::validation::Limits::default();
            // Bound the range before fetching a FlatBuffer. Payload validation
            // alone would still permit an attacker to request an 8 MiB block.
            let max_record = limits
                .max_bytes
                .checked_add(key.len())
                .and_then(|length| length.checked_add(1024))
                .ok_or(ArchiveError::Invalid)?;
            if key.len() > limits.max_bytes || pointer.length > max_record {
                return Err(ArchiveError::Invalid);
            }
            let bytes = self.index.bytes(pointer).await?;
            let record = checked_record(&bytes, &key, Kind::Definition)?;
            let contract =
                crate::validation::Contract::decode(payload(record)?, limits)
                    .map_err(|_| ArchiveError::Invalid)?;
            if contract.owner() != owner {
                return Err(ArchiveError::Invalid);
            }
            Ok(Some(contract))
        }
        .await;
        match result {
            Ok(Some(contract)) => Some(contract),
            Ok(None) | Err(_) => {
                unavailable();
                None
            },
        }
    }

    /// Borrows archive pins for comparison with independently approved inputs.
    ///
    /// These values describe the artifact; they do not approve themselves.
    /// The host must compare authority digests and module generations with
    /// its trusted configuration before supplying runtime evidence.
    #[must_use]
    pub fn versions(&self) -> &crate::authorization::Versions {
        self.authority.versions()
    }

    /// Loads verified obligations for the trusted host's evidence resolver.
    ///
    /// This integration API contains private metadata. It must never be
    /// returned to an untrusted client; use authorized `describe`, `read`, or
    /// `export_schema` for disclosure. No permission decision is cached here.
    pub async fn requirements(
        &self,
        route: &crate::authorization::Route,
    ) -> Result<
        BTreeMap<
            crate::authorization::Route,
            crate::authorization::Descriptor,
        >,
        ArchiveError,
    > {
        let primary = self
            .index
            .route(route)
            .await?
            .ok_or(ArchiveError::Invalid)?;
        let mut routes = self.index.prerequisites(route, &primary).await?;
        routes.insert(route.clone(), primary);
        #[cfg(feature = "export")]
        if matches!(
            route.operation,
            Operation::Describe | Operation::DescribeTransfer
        ) {
            let mut pending = vec![(route.clone(), true)];
            let mut expanded = std::collections::BTreeSet::new();
            while let Some((candidate, expand)) = pending.pop() {
                if routes.len() > 4096 {
                    return Err(ArchiveError::Invalid);
                }
                if !routes.contains_key(&candidate) {
                    let descriptor = self
                        .index
                        .route(&candidate)
                        .await?
                        .ok_or(ArchiveError::Invalid)?;
                    routes.extend(
                        self.index
                            .prerequisites(&candidate, &descriptor)
                            .await?,
                    );
                    routes.insert(candidate.clone(), descriptor);
                }
                let crate::Reference::Symbol(owner) = &candidate.target else {
                    continue;
                };
                if !expand || !expanded.insert(owner.clone()) {
                    continue;
                }
                let bytes = self.definition_bytes(owner).await?;
                let (fields, references) =
                    crate::export::description_inventory(&bytes)?;
                pending.extend(fields.into_iter().map(|name| {
                    (
                        Route::new(
                            crate::FieldRef::new(owner.clone(), name),
                            route.operation,
                        ),
                        false,
                    )
                }));
                pending.extend(references.into_iter().map(
                    |(target, expand)| {
                        (Route::new(target, route.operation), expand)
                    },
                ));
            }
        }
        Ok(routes)
    }

    /// Verifies the manifest and reads the shared authority block.
    pub fn open(source: R, pin: ArchivePin) -> Result<Self, ArchiveError> {
        let mut opening = std::pin::pin!(Self::open_mode(source, pin, true));
        // The synchronous mode uses only ready local reads, even when a
        // caller implements both methods on its range source.
        match opening
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop()))
        {
            Poll::Ready(result) => result,
            Poll::Pending => Err(ArchiveError::Invalid),
        }
    }

    /// Opens a snapshot without blocking on network reads.
    pub async fn open_async(
        source: R,
        pin: ArchivePin,
    ) -> Result<Self, ArchiveError> {
        Self::open_mode(source, pin, false).await
    }

    async fn open_mode(
        source: R,
        pin: ArchivePin,
        synchronous: bool,
    ) -> Result<Self, ArchiveError> {
        let range = RangeRequest {
            offset: 0,
            length: HEADER_LEN,
            sha256: pin.0,
        };
        let bytes = if synchronous {
            sync_fetch(&source, range)?
        } else {
            source.fetch(range).await?
        };
        let root = header(
            bytes
                .as_ref()
                .try_into()
                .map_err(|_| ArchiveError::Invalid)?,
            source.length()?,
            pin,
        )?;
        let index = Index {
            root,
            source,
            synchronous,
            pages: Mutex::new(BTreeMap::new()),
        };
        let authority = index
            .record(AUTHORITY, Kind::Authority, |record| {
                auth::ArchivedAuthority::decode(payload(record)?)
            })
            .await?
            .ok_or(ArchiveError::Invalid)?;
        Ok(Self { authority, index })
    }

    /// Returns only the requested and currently authorized symbol or field.
    ///
    /// Missing, denied and unreadable records all produce the same result.
    pub async fn read(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Symbol> {
        self.checked_symbol(access, runtime, provider)
            .await
            .unwrap_or_else(|_| {
                unavailable();
                None
            })
    }

    /// Holds the same IFC gate through an actual host effect, using only the
    /// archived security routes. The host binds the callback to this resource
    /// and supplies complete input provenance; this does not sandbox code.
    pub async fn perform<T, E, F: Future<Output = Result<T, E>>>(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
        effect: impl FnOnce() -> F,
    ) -> Result<EffectReceipt<T>, EffectError> {
        let routes = self.effect_routes(&access.route).await?;
        self.authority
            .perform(access, &routes, runtime, provider, effect)
            .await
    }

    /// Observes a protected effect receipt after fresh archived read checks.
    pub async fn inspect<'a, T>(
        &self,
        receipt: &'a EffectReceipt<T>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<&'a Result<T, EffectError>, EffectError> {
        let routes = self.effect_routes(&receipt.observation_route()).await?;
        self.authority
            .inspect(receipt, &routes, runtime, provider)
            .await
    }

    async fn effect_routes(
        &self,
        route: &Route,
    ) -> Result<BTreeMap<Route, Descriptor>, EffectError> {
        let load = async {
            let primary = self
                .index
                .route(route)
                .await?
                .ok_or(ArchiveError::Invalid)?;
            let mut routes = self.index.prerequisites(route, &primary).await?;
            routes.insert(route.clone(), primary);
            Ok::<_, ArchiveError>(routes)
        };
        load.await.map_err(|_| {
            unavailable();
            EffectError::Denied(Denial::Route)
        })
    }

    /// Releases a projection only if every requested item can be disclosed.
    pub async fn project(
        &self,
        items: &[Access],
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Vec<Symbol>> {
        let first = items.first()?;
        let mut result = Vec::with_capacity(items.len());
        for item in items {
            if !same_scope(first, item) {
                return None;
            }
            result.push(self.read(item, runtime, provider).await?);
        }
        Some(result)
    }

    /// Selects visible records from a caller's candidate set.
    ///
    /// This API does not walk the raw index, reveal the total symbol count,
    /// or report which candidates were unknown instead of denied.
    pub async fn discover(
        &self,
        candidates: &[Access],
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Vec<Symbol>> {
        let Some(first) = candidates.first() else {
            return Some(Vec::new());
        };
        let mut visible = Vec::new();
        for candidate in candidates {
            if !same_scope(first, candidate) {
                return None;
            }
            match self.checked_symbol(candidate, runtime, provider).await {
                Ok(Some(symbol)) => visible.push(symbol),
                Ok(None) => {},
                Err(_) => {
                    unavailable();
                    return None;
                },
            }
        }
        Some(visible)
    }

    /// Selects visible links among candidate endpoints after checking both
    /// ends.
    pub async fn links(
        &self,
        anchor: &Access,
        candidates: &[Access],
        direction: Direction,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Option<Vec<Symbol>> {
        self.read(anchor, runtime, provider).await?;
        let kind = match direction {
            Direction::Direct => Kind::Direct,
            Direction::Inverse => Kind::Inverse,
            Direction::Dependency => Kind::Dependency,
        };
        let mut visible = Vec::new();
        for candidate in candidates {
            if !same_scope(anchor, candidate) {
                return None;
            }
            let symbol = match self
                .checked_symbol(candidate, runtime, provider)
                .await
            {
                Ok(Some(symbol)) => symbol,
                Ok(None) => continue,
                Err(_) => {
                    unavailable();
                    return None;
                },
            };
            let key =
                link_key(kind, &anchor.route.target, &candidate.route.target);
            match self.index.record(&key, kind, |_| Ok(())).await {
                Ok(Some(_)) => visible.push(symbol),
                Ok(None) => {},
                Err(_) => {
                    unavailable();
                    return None;
                },
            }
        }
        Some(visible)
    }

    async fn checked_symbol(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<Option<Symbol>, ArchiveError> {
        if !matches!(
            access.route.operation,
            Operation::Read | Operation::Transfer
        ) {
            return Ok(None);
        }
        if !self.permitted(access, runtime, provider).await? {
            return Ok(None);
        }
        let key = format!("s/{}", target_key(&access.route.target));
        let symbol = self
            .index
            .record(&key, Kind::Symbol, |record| {
                let kind = DECLARATIONS
                    .iter()
                    .find_map(|(tag, kind)| {
                        (*tag == record.declaration()).then_some(*kind)
                    })
                    .ok_or(ArchiveError::Invalid)?;
                let primitive = match record.scalar() {
                    ScalarTag::Unset => None,
                    scalar => Some(
                        SCALARS
                            .iter()
                            .find_map(|(tag, kind)| {
                                (*tag == scalar).then_some(*kind)
                            })
                            .ok_or(ArchiveError::Invalid)?,
                    ),
                };
                Ok(Symbol {
                    target: access.route.target.clone(),
                    kind,
                    primitive,
                })
            })
            .await?
            .ok_or(ArchiveError::Invalid)?;
        Ok(Some(symbol))
    }

    /// Checks a route without disclosing a record or performing its effect.
    /// Hosts must retain their computation boundary through the actual I/O;
    /// this decision alone is not an effect capability.
    pub async fn authorize(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Decision {
        match self.permitted(access, runtime, provider).await {
            Ok(true) => Decision::Allow,
            _ => Decision::Deny(Denial::Runtime),
        }
    }

    pub(crate) async fn permitted(
        &self,
        access: &Access,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<bool, ArchiveError> {
        let Some(primary) = self.index.route(&access.route).await? else {
            return Ok(false);
        };
        let mut routes =
            self.index.prerequisites(&access.route, &primary).await?;
        routes.insert(access.route.clone(), primary);
        Ok(self
            .authority
            .authorize(access, &routes, runtime, provider)
            .await ==
            Decision::Allow)
    }

    #[cfg(feature = "export")]
    pub(crate) async fn definition_bytes(
        &self,
        owner: &crate::SymbolRef,
    ) -> Result<Vec<u8>, ArchiveError> {
        let key = format!(
            "d/{}",
            target_key(&crate::Reference::Symbol(owner.clone()))
        );
        self.index
            .record(&key, Kind::Definition, |record| {
                Ok(payload(record)?.to_vec())
            })
            .await?
            .ok_or(ArchiveError::Invalid)
    }
}

fn unavailable() {
    tracing::debug!(event.name = "gol.archive.unavailable");
}

fn same_scope(first: &Access, item: &Access) -> bool {
    first.instance.tenant == item.instance.tenant &&
        first.destination == item.destination &&
        first.purpose == item.purpose &&
        first.route.operation == item.route.operation
}

type Pages = BTreeMap<u64, (Pointer, Arc<[u8]>)>;

struct Index<R> {
    root: Pointer,
    source: R,
    synchronous: bool,
    pages: Mutex<Pages>,
}

impl<R: RangeReader> Index<R> {
    async fn bytes(
        &self,
        pointer: Pointer,
    ) -> Result<Arc<[u8]>, ArchiveError> {
        let range = request(pointer);
        let bytes = if self.synchronous {
            sync_fetch(&self.source, range)?
        } else {
            self.source.fetch(range).await?
        };
        verify(range, &bytes)?;
        tracing::trace!(
            event.name = "gol.archive.range",
            byte_count = pointer.length
        );
        Ok(bytes)
    }

    async fn page(&self, pointer: Pointer) -> Result<Arc<[u8]>, ArchiveError> {
        if !self.source.cache_index() {
            return self.bytes(pointer).await;
        }
        if let Some((known, bytes)) = self
            .pages
            .lock()
            .map_err(|_| ArchiveError::Invalid)?
            .get(&pointer.offset)
        {
            return if *known == pointer {
                Ok(Arc::clone(bytes))
            } else {
                Err(ArchiveError::Invalid)
            };
        }
        let bytes = self.bytes(pointer).await?;
        let mut cache =
            self.pages.lock().map_err(|_| ArchiveError::Invalid)?;
        // Bound warm index memory independently of the largest data block.
        const MAX_CACHED_PAGES: usize = 64;
        const MAX_CACHED_PAGE_BYTES: usize = 16 * 1024;
        if cache.len() < MAX_CACHED_PAGES &&
            bytes.len() <= MAX_CACHED_PAGE_BYTES
        {
            cache.insert(pointer.offset, (pointer, Arc::clone(&bytes)));
        }
        Ok(bytes)
    }

    async fn record<T>(
        &self,
        key: &str,
        kind: Kind,
        consume: impl FnOnce(Block<'_>) -> Result<T, ArchiveError>,
    ) -> Result<Option<T>, ArchiveError> {
        let Some(pointer) = self.locate(key).await? else {
            return Ok(None);
        };
        let bytes = self.bytes(pointer).await?;
        consume(checked_record(&bytes, key, kind)?).map(Some)
    }

    async fn route(
        &self,
        route: &Route,
    ) -> Result<Option<Descriptor>, ArchiveError> {
        self.record(&route_key(route), Kind::Route, |record| {
            auth::decode_route(payload(record)?)
        })
        .await
    }

    async fn prerequisites(
        &self,
        root: &Route,
        descriptor: &Descriptor,
    ) -> Result<BTreeMap<Route, Descriptor>, ArchiveError> {
        let dependencies = descriptor
            .dependencies()
            .iter()
            .filter(|route| *route != root);
        let mut result = BTreeMap::new();
        #[cfg(feature = "remote")]
        if !self.synchronous && self.source.parallelism() > 1 {
            let deps: Vec<_> = dependencies.clone().collect();
            for batch in deps.chunks(self.source.parallelism().clamp(1, 8)) {
                let keys: Vec<_> =
                    batch.iter().map(|route| route_key(route)).collect();
                let ranges = futures::future::try_join_all(keys.iter().map(
                    |key| async {
                        self.locate(key)
                            .await?
                            .map(request)
                            .ok_or(ArchiveError::Invalid)
                    },
                ))
                .await?;
                let buffers = self.source.fetch_many(&ranges).await?;
                if buffers.len() != ranges.len() {
                    return Err(ArchiveError::Invalid);
                }
                for (((route, key), range), bytes) in
                    batch.iter().zip(keys).zip(ranges).zip(buffers)
                {
                    verify(range, &bytes)?;
                    let record = checked_record(&bytes, &key, Kind::Route)?;
                    result.insert(
                        (*route).clone(),
                        auth::decode_route(payload(record)?)?,
                    );
                }
            }
            return Ok(result);
        }
        for route in dependencies {
            result.insert(
                route.clone(),
                self.route(route).await?.ok_or(ArchiveError::Invalid)?,
            );
        }
        Ok(result)
    }

    async fn locate(
        &self,
        key: &str,
    ) -> Result<Option<Pointer>, ArchiveError> {
        let mut pointer = self.root;
        let mut expected_level = None;
        loop {
            let bytes = self.page(pointer).await?;
            let directory = block(&bytes)?;
            let level = directory.level();
            let entries = directory.entries().ok_or(ArchiveError::Invalid)?;
            if directory.kind() != Kind::Directory ||
                directory.payload().is_some() ||
                directory.key().is_some() ||
                has_tags(directory) ||
                level > MAX_LEVEL ||
                expected_level.is_some_and(|expected| level != expected) ||
                entries.is_empty() ||
                entries.len() > PAGE_SIZE
            {
                return Err(ArchiveError::Invalid);
            }
            let mut previous = None;
            let mut selected = None;
            for entry in entries {
                let upper = entry.key();
                let child = Pointer::entry(entry, pointer.offset)?;
                if previous.is_some_and(|previous| previous >= upper) ||
                    upper.is_empty()
                {
                    return Err(ArchiveError::Invalid);
                }
                if selected.is_none() &&
                    (upper == key || (level != 0 && upper > key))
                {
                    selected = Some(child);
                }
                previous = Some(upper);
            }
            let Some(child) = selected else {
                return Ok(None);
            };
            if level == 0 {
                return Ok(Some(child));
            }
            pointer = child;
            expected_level = Some(level - 1);
        }
    }
}

fn request(pointer: Pointer) -> RangeRequest {
    RangeRequest {
        offset: pointer.offset,
        length: pointer.length,
        sha256: pointer.sha256,
    }
}

fn verify(range: RangeRequest, bytes: &[u8]) -> Result<(), ArchiveError> {
    if bytes.len() != range.length || digest(bytes) != range.sha256 {
        return Err(ArchiveError::Invalid);
    }
    Ok(())
}

fn checked_record<'a>(
    bytes: &'a [u8],
    key: &str,
    kind: Kind,
) -> Result<Block<'a>, ArchiveError> {
    let record = block(bytes)?;
    if record.kind() != kind ||
        record.level() != 0 ||
        record.key() != Some(key) ||
        record.entries().is_some() ||
        record.payload().is_some() == (kind == Kind::Symbol) ||
        (kind != Kind::Symbol && has_tags(record))
    {
        return Err(ArchiveError::Invalid);
    }
    Ok(record)
}
fn has_tags(record: Block<'_>) -> bool {
    record.declaration() != DeclarationTag::Unset ||
        record.scalar() != ScalarTag::Unset
}

fn payload(record: Block<'_>) -> Result<&[u8], ArchiveError> {
    Ok(record.payload().ok_or(ArchiveError::Invalid)?.bytes())
}
