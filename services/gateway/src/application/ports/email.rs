//! Email connection authority belongs to the mail service, independently of
//! pipelines.

use anyhow::Result;
use serde_json::Value;

#[async_trait::async_trait]
pub trait EmailService: Send + Sync {
    fn authenticates(&self, supplied: &str) -> bool;

    async fn operation(
        &self,
        tenant: &str,
        user: &str,
        request: &Value,
        delegated: bool,
    ) -> Result<Value>;
}
