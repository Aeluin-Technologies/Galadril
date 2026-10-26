//! Compilation delegates live authority without giving Scribe storage access.
use anyhow::Result;
use galadril_document_compiler::tools::{CompileInput, TemplateOperation};
use serde_json::Value;

#[async_trait::async_trait]
pub trait DocumentCompiler: Send + Sync {
    async fn templates(
        &self,
        capability: &str,
        operation: TemplateOperation,
    ) -> Result<Value>;
    async fn compile(
        &self,
        capability: &str,
        input: CompileInput,
    ) -> Result<Value>;
    async fn job(
        &self,
        capability: &str,
        id: &str,
        cancel: bool,
    ) -> Result<Value>;
    async fn template(
        &self,
        capability: &str,
        id: &str,
        version: &str,
    ) -> Result<galadril_document_compiler::domain::Template>;
    async fn pdf(&self, capability: &str, id: &str) -> Result<Vec<u8>>;
}
