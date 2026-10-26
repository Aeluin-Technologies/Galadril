//! Fixed deployment endpoints resolve existing Gateway capabilities on every
//! use.
use std::time::Duration;

use anyhow::{Result, ensure};
use galadril_document_compiler::delegation::{AuthorizationRequest, Grant};

use crate::ports::Authorizer;

pub struct Gateway {
    client: reqwest::Client,
    endpoint: String,
}

impl Gateway {
    pub fn new(endpoint: &str) -> Result<Self> {
        let url = reqwest::Url::parse(endpoint)?;
        ensure!(
            matches!(url.scheme(), "http" | "https") &&
                url.host_str().is_some() &&
                url.username().is_empty() &&
                url.password().is_none() &&
                url.query().is_none() &&
                url.fragment().is_none() &&
                matches!(url.path(), "" | "/"),
            "Invalid Gateway endpoint"
        );
        Ok(Self {
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(3))
                .timeout(Duration::from_secs(10))
                .build()?,
            endpoint: format!(
                "{}/internal/document-compiler/authorize",
                endpoint.trim_end_matches('/')
            ),
        })
    }
}

#[async_trait::async_trait]
impl Authorizer for Gateway {
    #[tracing::instrument(skip_all, name = "document_compiler.authorize")]
    async fn authorize(
        &self,
        capability: &str,
        request: AuthorizationRequest,
    ) -> Result<Grant> {
        let mut response = self
            .client
            .post(&self.endpoint)
            .bearer_auth(capability)
            .json(&request)
            .send()
            .await?;
        ensure!(response.status().is_success(), "Delegation denied");
        let mut bytes = Vec::with_capacity(1024);
        while let Some(chunk) = response.chunk().await? {
            ensure!(
                bytes.len() + chunk.len() <= 4096,
                "Delegation response exceeds limit"
            );
            bytes.extend_from_slice(&chunk);
        }
        let grant: Grant = serde_json::from_slice(&bytes)?;
        galadril_document_compiler::domain::validate_tenant(&grant.tenant)?;
        ensure!(
            !grant.actor.is_empty() &&
                grant.actor.len() <= 256 &&
                !grant.generation.is_empty() &&
                grant.generation.len() <= 64,
            "Invalid delegation"
        );
        Ok(grant)
    }
}
