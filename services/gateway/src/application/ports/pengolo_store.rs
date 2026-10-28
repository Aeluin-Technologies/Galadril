//! Tenant-scoped workspace persistence and ordered generation output.

use std::sync::Arc;

use anyhow::Result;

use crate::domain::pengolo::{OutputEvent, Record, SendInput};

/// An internal invalidation contains scope only, never document metadata.
#[derive(Clone, serde::Deserialize)]
pub struct LibraryScope {
    pub tenant: String,
    pub owner: String,
    pub project: Option<String>,
}

#[derive(Clone)]
pub enum LibraryChange {
    Scope(Arc<LibraryScope>),
    Resync,
}

impl LibraryChange {
    /// Keeps unrelated personal libraries and tenants off the client stream.
    pub fn matches(
        &self,
        tenant: &str,
        owner: &str,
        project: Option<&str>,
    ) -> bool {
        match self {
            Self::Resync => true,
            Self::Scope(scope) => {
                scope.tenant == tenant &&
                    scope.project.as_deref() == project &&
                    (project.is_some() || scope.owner == owner)
            },
        }
    }
}

#[derive(Clone, Copy)]
pub enum Collection {
    Preference,
    Agent,
    Chat,
    Project,
    File,
    Generation,
}

impl Collection {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Preference => "preference",
            Self::Agent => "agent",
            Self::Chat => "chat",
            Self::Project => "project",
            Self::File => "file",
            Self::Generation => "generation",
        }
    }
}

pub struct Reservation {
    pub chat: Record,
    pub generation: Record,
    pub created: bool,
}

pub struct ObjectExpiry {
    pub id: String,
    pub staging: bool,
    pub temporary: bool,
}

pub struct StagedFile<'a> {
    pub generated: bool,
    pub tenant: &'a str,
    pub owner: &'a str,
    pub source: &'a str,
    pub destination: &'a str,
    pub mime: &'a str,
    pub size: i64,
    pub name: &'a str,
    pub checksum: &'a str,
}

pub struct RecordQuery<'a> {
    pub tenant: &'a str,
    pub collection: Collection,
    pub project: Option<&'a str>,
    pub owner: &'a str,
    pub search: &'a str,
    pub after: Option<&'a str>,
    pub limit: usize,
}

#[async_trait::async_trait]
pub trait PengoloStore: Send + Sync {
    fn library_changes(
        &self,
    ) -> Result<tokio::sync::broadcast::Receiver<LibraryChange>> {
        anyhow::bail!("Library notifications unavailable")
    }
    /// Serializes immutable promotion with deletion and expiration across
    /// Gateways.
    async fn promote_file(
        &self,
        tenant: &str,
        record: &Record,
        temporary: bool,
        file: &StagedFile<'_>,
        objects: &dyn PengoloObjects,
    ) -> Result<Record>;
    /// Renews durable retention only after an authorized content read.
    async fn refresh_file_retention(
        &self,
        tenant: &str,
        record: &Record,
        temporary: bool,
    ) -> Result<Record>;
    /// Expires the current authorized source without replacing concurrent
    /// index metadata.
    async fn expire_file(
        &self,
        tenant: &str,
        record: &Record,
        temporary: bool,
    ) -> Result<()>;
    /// Rechecks a deadline under the same lock used by file promotion.
    async fn purge_file(
        &self,
        tenant: &str,
        id: &str,
        temporary: bool,
        staging: bool,
        objects: &dyn PengoloObjects,
    ) -> Result<bool>;
    async fn get(
        &self,
        tenant: &str,
        collection: Collection,
        id: &str,
        temporary: bool,
    ) -> Result<Option<Record>>;
    async fn list(
        &self,
        tenant: &str,
        collection: Collection,
        after: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Record>>;
    async fn reserve_file(
        &self,
        tenant: &str,
        record: &Record,
        temporary: bool,
    ) -> Result<Record>;
    async fn save_project(
        &self,
        tenant: &str,
        record: &Record,
        expected: Option<i64>,
        access: &dyn crate::application::ports::access_store::AccessStore,
    ) -> Result<Record>;
    async fn expire_temporary(
        &self,
        tenant: &str,
        owner: &str,
        session: &str,
    ) -> Result<()>;
    async fn list_scoped(&self, query: RecordQuery<'_>)
    -> Result<Vec<Record>>;
    async fn save(
        &self,
        tenant: &str,
        collection: Collection,
        record: &Record,
        temporary: bool,
        expected_revision: Option<i64>,
    ) -> Result<Record>;
    async fn remove(
        &self,
        tenant: &str,
        collection: Collection,
        id: &str,
        temporary: bool,
    ) -> Result<()>;
    async fn reserve(
        &self,
        tenant: &str,
        actor: &str,
        input: &SendInput,
        capability: &str,
    ) -> Result<Reservation>;
    async fn append(
        &self,
        tenant: &str,
        generation: &str,
        temporary: bool,
        event: &OutputEvent,
    ) -> Result<()>;
    async fn events(
        &self,
        tenant: &str,
        generation: &str,
        temporary: bool,
        after: i32,
    ) -> Result<Vec<OutputEvent>>;
    async fn resolve_capability(
        &self,
        capability: &str,
    ) -> Result<(String, Record, bool)>;
    async fn generation_capability(
        &self,
        tenant: &str,
        id: &str,
    ) -> Result<Option<String>>;
    async fn record_evidence(
        &self,
        tenant: &str,
        generation: &str,
        temporary: bool,
        sources: &[crate::application::ports::conversation_store::EvidenceSource],
    ) -> Result<()>;
    async fn expire_leases(&self, tenant: &str) -> Result<()>;
    async fn expired_objects(&self, tenant: &str)
    -> Result<Vec<ObjectExpiry>>;
    async fn cleanup_tenants(&self) -> Result<Vec<String>>;
    async fn expired(
        &self,
        tenant: &str,
    ) -> Result<Vec<(Collection, Record, bool)>>;
}

pub type FileChunks = std::pin::Pin<
    Box<dyn futures::Stream<Item = Result<axum::body::Bytes>> + Send>,
>;

#[async_trait::async_trait]
pub trait PengoloObjects: Send + Sync {
    async fn write_staging(
        &self,
        file: &StagedFile<'_>,
        data: axum::body::Bytes,
    ) -> Result<()>;
    async fn upload_url(
        &self,
        file: &StagedFile<'_>,
    ) -> Result<(String, std::collections::BTreeMap<String, String>)>;
    async fn finalize(&self, file: &StagedFile<'_>) -> Result<i64>;
    async fn delete_staging(&self, key: &str) -> Result<()>;
    async fn read(&self, key: &str) -> Result<FileChunks>;
    async fn download_url(
        &self,
        key: &str,
        ttl_seconds: u64,
    ) -> Result<String>;
    async fn delete(&self, key: &str) -> Result<()>;
}
