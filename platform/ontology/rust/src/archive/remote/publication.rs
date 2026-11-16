//! The live manifest serializes activation and retirement across processes.

use std::collections::BTreeMap;
use std::sync::atomic::Ordering::Relaxed;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::time::Instant;

use super::s3::Condition;
use super::{Artifact, Generation, RemoteError, RemoteOptions, RemoteStore};
#[cfg(feature = "parser")]
use super::{Window, within};

const MANIFEST: &str = "manifest.json";
const MANIFEST_BYTES: usize = 64 * 1024;
const MAX_PENDING: usize = 256;
const CAS_TRIES: usize = 16;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Manifest {
    version: u32,
    reader_ms: u64,
    publisher_ms: u64,
    // Every CAS needs a fresh ETag, including when an interleaving restores
    // the previous semantic state.
    revision: Generation,
    pub active: Option<Artifact>,
    pending: BTreeMap<Generation, Status>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
enum Status {
    Staging,
    Retired,
    Deleting,
}

pub(super) struct Current {
    pub state: Manifest,
    etag: Option<Box<str>>,
}

/// An uploaded generation awaiting activation.
///
/// Dropping this token leaves the active manifest intact. The reaper can
/// reclaim the staged object after the publication window.
#[cfg(feature = "parser")]
pub struct Staged {
    store: RemoteStore,
    artifact: Artifact,
    expected: Option<Generation>,
    lifetime: Window,
}

/// A local observation that must span a complete retirement interval.
///
/// A restarted worker can observe the same entries and begin a new interval.
/// Object keys are never reused.
pub struct Cleanup {
    store: RemoteStore,
    observed: Instant,
    candidates: BTreeMap<Generation, Status>,
}

impl Manifest {
    fn new(options: &RemoteOptions) -> Self {
        Self {
            version: 1,
            reader_ms: options.reader_window.as_millis() as u64,
            publisher_ms: options.publication_window.as_millis() as u64,
            revision: Generation::new(),
            active: None,
            pending: BTreeMap::new(),
        }
    }

    fn validate(&self, options: &RemoteOptions) -> Result<(), RemoteError> {
        if self.version != 1 ||
            u128::from(self.reader_ms) != options.reader_window.as_millis() ||
            u128::from(self.publisher_ms) !=
                options.publication_window.as_millis()
        {
            return Err(RemoteError::Configuration);
        }
        if self.pending.len() > MAX_PENDING ||
            self.active.as_ref().is_some_and(|artifact| {
                self.pending.contains_key(&artifact.id) ||
                    artifact.etag.is_empty() ||
                    artifact.bytes <
                        crate::archive::container::HEADER_LEN as u64
            })
        {
            return Err(RemoteError::Integrity);
        }
        Ok(())
    }
}

impl RemoteStore {
    pub(super) async fn load(&self) -> Result<Current, RemoteError> {
        self.counters.manifest_requests.fetch_add(1, Relaxed);
        match self.storage.get(MANIFEST, MANIFEST_BYTES).await? {
            Some((bytes, etag)) => {
                self.counters
                    .manifest_bytes
                    .fetch_add(bytes.len() as u64, Relaxed);
                let state: Manifest = super::super::decode(&bytes)
                    .map_err(|_| RemoteError::Integrity)?;
                state.validate(&self.options)?;
                Ok(Current {
                    state,
                    etag: Some(etag),
                })
            },
            None => Ok(Current {
                state: Manifest::new(&self.options),
                etag: None,
            }),
        }
    }

    async fn commit(&self, current: &mut Current) -> Result<(), RemoteError> {
        current.state.revision = Generation::new();
        let payload = serde_json::to_vec(&current.state)
            .map_err(|_| RemoteError::Integrity)?;
        if payload.len() > MANIFEST_BYTES {
            return Err(RemoteError::Capacity);
        }
        let when = current
            .etag
            .as_deref()
            .map_or(Condition::Create, Condition::Matches);
        self.storage.put(MANIFEST, payload, when).await?;
        Ok(())
    }

    #[cfg(feature = "parser")]
    async fn reserve(
        &self,
        expected: Option<Generation>,
        id: Generation,
    ) -> Result<(), RemoteError> {
        for _ in 0..CAS_TRIES {
            let mut current = self.load().await?;
            if current.state.active.as_ref().map(|artifact| artifact.id) !=
                expected ||
                current.state.pending.contains_key(&id)
            {
                return Err(RemoteError::Conflict);
            }
            if current.state.pending.len() == MAX_PENDING {
                return Err(RemoteError::Capacity);
            }
            current.state.pending.insert(id, Status::Staging);
            match self.commit(&mut current).await {
                Err(RemoteError::Conflict) => continue,
                result => return result,
            }
        }
        Err(RemoteError::Conflict)
    }

    /// Uploads and verifies the archive after reserving its generation.
    /// Activation checks the expected generation again.
    #[cfg(feature = "parser")]
    pub async fn stage(
        &self,
        archive: &super::super::CompiledArchive,
        expected: Option<Generation>,
    ) -> Result<Staged, RemoteError> {
        let lifetime = Window::new(self.options.publication_window);
        within(lifetime, async {
            self.storage.ensure_unversioned().await?;
            let id = Generation::new();
            self.reserve(expected, id).await?;
            let bytes = archive.bytes();
            let key = artifact_key(id);
            let etag = self
                .storage
                .put(&key, bytes.to_vec(), Condition::Create)
                .await?;
            self.storage.verify(&key, bytes, &etag).await?;
            tracing::debug!(
                event.name = "gol.remote.stage_verified",
                byte_count = bytes.len()
            );
            Ok(Staged {
                store: self.clone(),
                artifact: Artifact {
                    id,
                    pin: archive.pin().0,
                    bytes: bytes.len() as u64,
                    etag,
                },
                expected,
                lifetime,
            })
        })
        .await
    }

    /// Publishes a checked artifact through the staged protocol.
    #[cfg(feature = "parser")]
    pub async fn publish(
        &self,
        archive: &super::super::CompiledArchive,
        expected: Option<Generation>,
    ) -> Result<Generation, RemoteError> {
        self.stage(archive, expected).await?.activate().await
    }

    /// Starts observing candidates for deferred cleanup.
    pub async fn observe_cleanup(&self) -> Result<Cleanup, RemoteError> {
        let current = self.load().await?;
        Ok(Cleanup {
            store: self.clone(),
            observed: Instant::now(),
            candidates: current.state.pending,
        })
    }

    async fn mark_deleting(
        &self,
        id: Generation,
        expected: Status,
    ) -> Result<bool, RemoteError> {
        for _ in 0..CAS_TRIES {
            let mut current = self.load().await?;
            match current.state.pending.get(&id) {
                Some(Status::Deleting) => return Ok(true),
                Some(status) if *status == expected => {},
                _ => return Ok(false),
            }
            current.state.pending.insert(id, Status::Deleting);
            match self.commit(&mut current).await {
                Ok(()) => return Ok(true),
                Err(RemoteError::Conflict) => continue,
                Err(error) => return Err(error),
            }
        }
        Err(RemoteError::Conflict)
    }

    async fn forget(&self, id: Generation) -> Result<(), RemoteError> {
        for _ in 0..CAS_TRIES {
            let mut current = self.load().await?;
            if current.state.pending.get(&id) != Some(&Status::Deleting) {
                return Ok(());
            }
            current.state.pending.remove(&id);
            match self.commit(&mut current).await {
                Err(RemoteError::Conflict) => continue,
                result => return result,
            }
        }
        Err(RemoteError::Conflict)
    }
}

#[cfg(feature = "parser")]
impl Staged {
    /// Switches the operational manifest by compare-and-swap.
    pub async fn activate(self) -> Result<Generation, RemoteError> {
        let id = self.artifact.id;
        within(self.lifetime, async {
            self.store.storage.ensure_unversioned().await?;
            for _ in 0..CAS_TRIES {
                let mut current = self.store.load().await?;
                if current.state.active.as_ref().map(|artifact| artifact.id) !=
                    self.expected ||
                    current.state.pending.get(&id) != Some(&Status::Staging)
                {
                    return Err(RemoteError::Conflict);
                }
                current.state.pending.remove(&id);
                if let Some(previous) =
                    current.state.active.replace(self.artifact.clone())
                {
                    current.state.pending.insert(previous.id, Status::Retired);
                }
                match self.store.commit(&mut current).await {
                    Ok(()) => {
                        tracing::info!(event.name = "gol.remote.activated");
                        return Ok(id);
                    },
                    Err(RemoteError::Conflict) => continue,
                    Err(error) => return Err(error),
                }
            }
            Err(RemoteError::Conflict)
        })
        .await
    }
}

impl Cleanup {
    /// Reclaims candidates whose observation covers their complete use
    /// window, including generations fenced by an interrupted sweep.
    pub async fn sweep(&self) -> Result<usize, RemoteError> {
        self.store.storage.ensure_unversioned().await?;
        let mut removed = 0;
        let mut retained = false;
        for (id, status) in &self.candidates {
            let grace = match status {
                Status::Staging => self.store.options.publication_window * 2,
                Status::Retired => self.store.options.reader_window * 2,
                Status::Deleting => Duration::ZERO,
            };
            if self.observed.elapsed() < grace {
                retained = true;
                continue;
            }
            if self.store.mark_deleting(*id, *status).await? {
                self.store.storage.delete(&artifact_key(*id)).await?;
                self.store.forget(*id).await?;
                removed += 1;
            }
        }
        // A timed-out PUT may arrive after deletion of its fenced key. Read
        // the manifest after listing so a new object cannot race its staging
        // registration.
        for (key, id) in self.store.storage.list_artifacts().await? {
            let current = self.store.load().await?;
            if current.etag.is_some() &&
                current
                    .state
                    .active
                    .as_ref()
                    .is_none_or(|active| active.id != id) &&
                !current.state.pending.contains_key(&id)
            {
                self.store.storage.delete(&key).await?;
                removed += 1;
            }
        }
        tracing::debug!(event.name = "gol.remote.cleanup", removed, retained);
        if removed == 0 && retained {
            return Err(RemoteError::Retained);
        }
        Ok(removed)
    }
}

pub(super) fn artifact_key(id: Generation) -> String {
    format!("artifacts/{id}.golc")
}
