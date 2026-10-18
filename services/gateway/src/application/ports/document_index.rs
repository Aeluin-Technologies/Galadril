//! Canonical document evidence is independent of conversation and ESKG
//! storage.

use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::domain::document_retrieval::{
    Feature, SearchHit, SearchInput, Unit,
};
use crate::domain::pengolo::Record;

#[derive(Debug, Clone)]
pub struct IndexJob {
    pub source_id: String,
    pub checksum: String,
    pub lease: String,
    pub file: Record,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FactHit {
    pub passage_id: String,
    pub ordinal: i32,
    pub fact: crate::domain::document_retrieval::Fact,
    pub similarity: f64,
}

pub struct Candidates {
    pub hits: Vec<SearchHit>,
    pub lexical: Vec<String>,
    pub dense: Vec<String>,
    pub facts: Vec<FactHit>,
    pub lexical_backend: String,
}

#[derive(Clone, Copy)]
pub struct IndexScope<'a> {
    pub tenant: &'a str,
    pub owner: &'a str,
    pub input: &'a SearchInput,
}

#[async_trait::async_trait]
pub trait DocumentIndex: Send + Sync {
    async fn enqueue(&self, tenant: &str, source: &str) -> Result<()>;
    async fn tenants_with_work(&self) -> Result<Vec<String>>;
    async fn claim(&self, tenant: &str) -> Result<Option<IndexJob>>;
    async fn renew(&self, tenant: &str, job: &IndexJob) -> Result<()>;
    async fn expand(
        &self,
        scope: IndexScope<'_>,
        seeds: &[FactHit],
    ) -> Result<Vec<SearchHit>>;
    async fn stage(
        &self,
        tenant: &str,
        job: &IndexJob,
        units: &[Unit],
        features: &[Feature],
        profile: Option<&str>,
    ) -> Result<()>;
    async fn publish(
        &self,
        tenant: &str,
        job: &IndexJob,
        profile: Option<&str>,
        warnings: &[String],
    ) -> Result<()>;
    async fn fail(
        &self,
        tenant: &str,
        job: &IndexJob,
        code: &str,
    ) -> Result<()>;
    async fn candidates(
        &self,
        scope: IndexScope<'_>,
        vector: Option<&[f32]>,
        profile: Option<&str>,
    ) -> Result<Candidates>;
}
