//! Ports keep network, persistence, and Linux execution outside orchestration.
use anyhow::Result;
use galadril_document_compiler::delegation::{AuthorizationRequest, Grant};
use galadril_document_compiler::domain::Rendered;

#[async_trait::async_trait]
pub trait ObjectStore: Send + Sync {
    async fn get(&self, key: &str, limit: usize) -> Result<Vec<u8>>;
    async fn exists(&self, key: &str) -> Result<bool>;
    async fn put_new(&self, key: &str, data: Vec<u8>) -> Result<()>;
    async fn delete(&self, key: &str) -> Result<()>;
    async fn list(
        &self,
        prefix: &str,
        after: &str,
    ) -> Result<(Vec<String>, String)>;
}

#[async_trait::async_trait]
pub trait Authorizer: Send + Sync {
    async fn authorize(
        &self,
        capability: &str,
        request: AuthorizationRequest,
    ) -> Result<Grant>;
}

#[async_trait::async_trait]
pub trait Sandbox: Send + Sync {
    async fn compile(&self, project: Rendered) -> Result<Vec<u8>>;
}

#[async_trait::async_trait]
pub trait JobQueue: Send + Sync {
    async fn expired(&self, now: i64) -> Result<Vec<crate::domain::Job>>;
    async fn forget(&self, job: crate::domain::Job) -> Result<()>;
    async fn enqueue(
        &self,
        job: crate::domain::NewJob,
        now: i64,
    ) -> Result<crate::domain::Job>;
    async fn claim(&self, now: i64) -> Result<Option<crate::domain::Job>>;
    async fn get(&self, tenant: &str, id: &str) -> Result<crate::domain::Job>;
    async fn cancel(
        &self,
        tenant: &str,
        id: &str,
    ) -> Result<crate::domain::Job>;
    async fn finish(
        &self,
        job: crate::domain::Job,
        succeeded: bool,
    ) -> Result<bool>;
}
