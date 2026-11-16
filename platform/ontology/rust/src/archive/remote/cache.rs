//! Cache authenticated fragments by archive generation and digest.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex};

use super::{Artifact, DiskCache, Generation, ReadMetrics};
use crate::archive::RangeRequest;

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Key {
    generation: Generation,
    archive: [u8; 32],
    digest: [u8; 32],
    offset: u64,
    length: usize,
}

impl Key {
    pub fn new(artifact: &Artifact, range: RangeRequest) -> Self {
        Self {
            generation: artifact.id,
            archive: artifact.pin,
            digest: range.sha256,
            offset: range.offset,
            length: range.length,
        }
    }
}

#[derive(Default)]
pub(super) struct Counters {
    pub requests: AtomicU64,
    pub bytes: AtomicU64,
    pub manifest_requests: AtomicU64,
    pub manifest_bytes: AtomicU64,
    ram_hits: AtomicU64,
    disk_hits: AtomicU64,
    misses: AtomicU64,
}

impl Counters {
    pub fn read(&self) -> ReadMetrics {
        ReadMetrics {
            requests: self.requests.load(Relaxed),
            bytes: self.bytes.load(Relaxed),
            manifest_requests: self.manifest_requests.load(Relaxed),
            manifest_bytes: self.manifest_bytes.load(Relaxed),
            ram_hits: self.ram_hits.load(Relaxed),
            disk_hits: self.disk_hits.load(Relaxed),
            misses: self.misses.load(Relaxed),
        }
    }
}

pub(super) struct Cache {
    ram: Mutex<Table<Arc<[u8]>>>,
    disk: Option<Arc<Disk>>,
}

struct Disk {
    directory: std::path::PathBuf,
    files: Mutex<Table<File>>,
}

impl Cache {
    pub fn new(ram_bytes: usize, disk: Option<DiskCache>) -> Self {
        Self {
            ram: Mutex::new(Table::new(ram_bytes)),
            disk: disk.map(|disk| {
                Arc::new(Disk {
                    directory: disk.directory,
                    files: Mutex::new(Table::new(disk.bytes)),
                })
            }),
        }
    }

    pub async fn get(
        &self,
        key: Key,
        counters: &Counters,
    ) -> Option<Arc<[u8]>> {
        let cached = match self.ram.lock() {
            Ok(mut ram) => ram.get(&key).cloned(),
            Err(_) => {
                tracing::debug!(
                    event.name = "gol.remote.ram_cache_unavailable"
                );
                None
            },
        };
        if let Some(bytes) = cached {
            counters.ram_hits.fetch_add(1, Relaxed);
            return Some(bytes);
        }
        if let Some(disk) = &self.disk {
            let disk = Arc::clone(disk);
            match tokio::task::spawn_blocking(move || disk.get(key)).await {
                Ok(Some(bytes)) => {
                    counters.disk_hits.fetch_add(1, Relaxed);
                    self.put_ram(key, &bytes);
                    return Some(bytes);
                },
                Ok(None) => {},
                Err(_) => tracing::debug!(
                    event.name = "gol.remote.disk_cache_unavailable"
                ),
            }
        }
        counters.misses.fetch_add(1, Relaxed);
        None
    }

    pub async fn put(&self, key: Key, bytes: &Arc<[u8]>) {
        self.put_ram(key, bytes);
        if let Some(disk) = &self.disk {
            let disk = Arc::clone(disk);
            let bytes = Arc::clone(bytes);
            match tokio::task::spawn_blocking(move || disk.put(key, &bytes))
                .await
            {
                Ok(Ok(())) => {},
                Ok(Err(_)) | Err(_) => tracing::debug!(
                    event.name = "gol.remote.disk_cache_unavailable"
                ),
            }
        }
    }

    fn put_ram(&self, key: Key, bytes: &Arc<[u8]>) {
        match self.ram.lock() {
            Ok(mut ram) => {
                if ram.reserve(key) {
                    ram.insert(key, Arc::clone(bytes));
                }
            },
            Err(_) => tracing::debug!(
                event.name = "gol.remote.ram_cache_unavailable"
            ),
        }
    }
}

impl Disk {
    fn get(&self, key: Key) -> Option<Arc<[u8]>> {
        let mut files = match self.files.lock() {
            Ok(files) => files,
            Err(_) => {
                tracing::debug!(
                    event.name = "gol.remote.disk_cache_unavailable"
                );
                return None;
            },
        };
        let file = files.get(&key)?;
        let mut bytes = vec![0; key.length];
        if file
            .seek(SeekFrom::Start(0))
            .and_then(|_| file.read_exact(&mut bytes))
            .is_ok()
        {
            return Some(bytes.into());
        }
        files.remove(key);
        tracing::debug!(event.name = "gol.remote.disk_cache_unavailable");
        None
    }

    fn put(&self, key: Key, bytes: &[u8]) -> std::io::Result<()> {
        let mut files = self
            .files
            .lock()
            .map_err(|_| std::io::Error::other("GOL cache lock"))?;
        if files.reserve(key) {
            // Anonymous files are reclaimed with their handles, including
            // when a process crashes with unused generations cached.
            let mut file = tempfile::tempfile_in(&self.directory)?;
            file.write_all(bytes)?;
            files.insert(key, file);
        }
        Ok(())
    }
}

struct Table<V> {
    entries: BTreeMap<Key, (u64, V)>,
    bytes: usize,
    limit: usize,
    clock: u64,
}

impl<V> Table<V> {
    fn new(limit: usize) -> Self {
        Self {
            entries: BTreeMap::new(),
            bytes: 0,
            limit,
            clock: 0,
        }
    }

    fn get(&mut self, key: &Key) -> Option<&mut V> {
        let (age, value) = self.entries.get_mut(key)?;
        self.clock = self.clock.saturating_add(1);
        *age = self.clock;
        Some(value)
    }

    fn reserve(&mut self, key: Key) -> bool {
        if key.length > self.limit || self.get(&key).is_some() {
            return false;
        }
        // The entry bound also limits SSD file descriptors.
        while self.bytes > self.limit - key.length || self.entries.len() >= 256
        {
            let Some(oldest) = self
                .entries
                .iter()
                .min_by_key(|(_, (age, _))| age)
                .map(|(key, _)| *key)
            else {
                return false;
            };
            self.remove(oldest);
        }
        true
    }

    fn remove(&mut self, key: Key) {
        if self.entries.remove(&key).is_some() {
            self.bytes -= key.length;
        }
    }

    fn insert(&mut self, key: Key, value: V) {
        self.clock = self.clock.saturating_add(1);
        self.entries.insert(key, (self.clock, value));
        self.bytes += key.length;
    }
}
