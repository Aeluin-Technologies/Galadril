//! Private email bridge keeps authenticated actors out of model arguments.

use std::time::Duration;

use anyhow::{Result, ensure};
use secrecy::{ExposeSecret, SecretString};
use serde_json::Value;

pub struct UtilsClient {
    client: reqwest::Client,
    endpoint: String,
    key: SecretString,
}

/// Models may prepare drafts but cannot grant permission or approve effects.
pub fn validate_tool_operation(request: &Value) -> Result<()> {
    ensure!(
        matches!(
            request.get("operation").and_then(Value::as_str),
            Some(
                "accounts" |
                    "contacts" |
                    "messages" |
                    "message" |
                    "drafts" |
                    "save_draft" |
                    "label"
            )
        ),
        "Email tool operation rejected"
    );
    Ok(())
}

impl UtilsClient {
    pub fn authenticates(&self, supplied: &str) -> bool {
        let expected = self.key.expose_secret().as_bytes();
        supplied.len() == expected.len() &&
            supplied
                .as_bytes()
                .iter()
                .zip(expected)
                .fold(0_u8, |difference, (left, right)| {
                    difference | (left ^ right)
                }) ==
                0
    }

    pub fn new(endpoint: &str, key: SecretString) -> Result<Self> {
        let url = reqwest::Url::parse(endpoint)?;
        ensure!(
            matches!(url.scheme(), "http" | "https") &&
                url.host_str().is_some() &&
                url.username().is_empty() &&
                url.password().is_none() &&
                url.query().is_none() &&
                url.fragment().is_none() &&
                matches!(url.path(), "" | "/") &&
                key.expose_secret().len() >= 32,
            "Invalid email service configuration"
        );
        Ok(Self {
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(5))
                .timeout(Duration::from_secs(180))
                .no_proxy()
                .build()?,
            endpoint: format!("{}/operation", endpoint.trim_end_matches('/')),
            key,
        })
    }

    /// This URL and credential come only from deployment configuration.
    pub fn from_environment() -> Result<Option<Self>> {
        let Ok(url) = std::env::var("UTILS_SERVICE_URL") else {
            return Ok(None);
        };
        let path = std::env::var("UTILS_SERVICE_KEY_FILE")?;
        let key = std::fs::read_to_string(path)?;
        Ok(Some(Self::new(
            &url,
            SecretString::from(key.trim().to_owned()),
        )?))
    }

    #[tracing::instrument(skip_all, name = "utils.email.bridge")]
    pub async fn operation(
        &self,
        tenant: &str,
        user: &str,
        request: &Value,
        delegated: bool,
    ) -> Result<Value> {
        if delegated {
            validate_tool_operation(request)?;
        }
        let body = serde_json::to_vec(request)?;
        ensure!(
            body.len() <= 16 * 1024 * 1024,
            "Email request exceeds limit"
        );
        let mut response = self
            .client
            .post(&self.endpoint)
            .header("x-utils-key", self.key.expose_secret())
            .header("x-utils-tenant", tenant)
            .header("x-utils-user", user)
            .header(
                "x-utils-delegated",
                if delegated { "true" } else { "false" },
            )
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(body)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("Email service unavailable"))?;
        if response.status() == reqwest::StatusCode::FORBIDDEN {
            anyhow::bail!("FORBIDDEN");
        }
        let success = response.status().is_success();
        let mut output = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| anyhow::anyhow!("Email response unavailable"))?
        {
            ensure!(
                output.len() + chunk.len() <= 16 * 1024 * 1024,
                "Email response exceeds limit"
            );
            output.extend_from_slice(&chunk);
        }
        if !success {
            let payload: Value =
                serde_json::from_slice(&output).unwrap_or(Value::Null);
            match payload.get("detail").and_then(Value::as_str) {
                Some("AUTHENTICATION_FAILED") => {
                    anyhow::bail!("AUTHENTICATION_FAILED")
                },
                Some("TLS_FAILED") => anyhow::bail!("TLS_FAILED"),
                Some("CONNECTION_UNAVAILABLE") => {
                    anyhow::bail!("CONNECTION_UNAVAILABLE")
                },
                _ => anyhow::bail!("Email operation rejected"),
            }
        }
        serde_json::from_slice(&output)
            .map_err(|_| anyhow::anyhow!("Invalid email service response"))
    }
}

#[async_trait::async_trait]
impl crate::application::ports::email::EmailService for UtilsClient {
    fn authenticates(&self, supplied: &str) -> bool {
        Self::authenticates(self, supplied)
    }

    async fn operation(
        &self,
        tenant: &str,
        user: &str,
        request: &Value,
        delegated: bool,
    ) -> Result<Value> {
        Self::operation(self, tenant, user, request, delegated).await
    }
}
