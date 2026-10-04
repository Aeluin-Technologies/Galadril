//! Private HTTP agent adapter; Gateway owns persistence and delegated
//! authority.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use futures::StreamExt as _;
use secrecy::{ExposeSecret as _, SecretString};
use serde::Deserialize;
use serde_json::json;

use crate::application::ports::conversation_agent::{
    AgentChunk, AgentRequest, AgentStream, ConversationAgent,
};
use crate::application::usecases::chat_tools::ChatTools;
use crate::config::ScribeRuntimeConfig;

pub struct ScribeAgent {
    client: reqwest::Client,
    endpoint: String,
    service_token: SecretString,
    tools: Arc<ChatTools>,
}

pub struct DisabledScribeAgent;

#[async_trait::async_trait]
impl ConversationAgent for DisabledScribeAgent {
    async fn start(&self, _request: AgentRequest<'_>) -> Result<AgentStream> {
        bail!("Scribe is disabled")
    }
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum RuntimeChunk {
    Content {
        content: String,
    },
    Completed {
        #[serde(default, rename = "content")]
        _content: String,
    },
    Failed {
        #[serde(default, rename = "content")]
        _content: String,
    },
}

impl ScribeAgent {
    pub fn new(
        config: &ScribeRuntimeConfig,
        tools: Arc<ChatTools>,
    ) -> Result<Arc<Self>> {
        let endpoint = reqwest::Url::parse(&config.endpoint)
            .context("Invalid Scribe endpoint")?;
        anyhow::ensure!(
            matches!(endpoint.scheme(), "http" | "https") &&
                endpoint.username().is_empty() &&
                endpoint.password().is_none() &&
                endpoint.query().is_none() &&
                endpoint.fragment().is_none(),
            "Invalid Scribe endpoint"
        );
        let service_token = config
            .service_token
            .as_ref()
            .context("scribe.service_token is required")?
            .clone();
        anyhow::ensure!(
            service_token.expose_secret().len() >= 32,
            "Scribe service token is too short"
        );
        Ok(Arc::new(Self {
            client: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(900))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            endpoint: format!(
                "{}/runs",
                endpoint.as_str().trim_end_matches('/')
            ),
            service_token,
            tools,
        }))
    }

    fn attachments(
        attachments: &[crate::application::ports::conversation_agent::AgentAttachment],
    ) -> Vec<serde_json::Value> {
        attachments.iter().map(|attachment| json!({"kind": attachment.kind.as_str(), "url": attachment.url})).collect()
    }
}

#[async_trait::async_trait]
impl ConversationAgent for ScribeAgent {
    async fn start(&self, request: AgentRequest<'_>) -> Result<AgentStream> {
        let lease = self.tools.delegate(&request)?;
        let history = request
            .history
            .iter()
            .map(|message| {
                json!({
                    "role": message.role.as_str(), "content": message.content,
                    "attachments": Self::attachments(&message.attachments),
                })
            })
            .collect::<Vec<_>>();
        let response = self.client.post(&self.endpoint)
            .bearer_auth(self.service_token.expose_secret())
            .json(&json!({"prompt": request.prompt, "model_alias": request.model_alias,
                "history": history, "attachments": Self::attachments(&request.attachments),
                "capability": lease.token()}))
            .send().await.context("Scribe transport failed")?;
        anyhow::ensure!(
            response.status().is_success(),
            "Scribe refused generation"
        );
        let stream = async_stream::try_stream! {
            let _lease = lease;
            let mut bytes = response.bytes_stream();
            let mut buffer = Vec::with_capacity(8192);
            let mut completed = false;
            while let Some(part) = bytes.next().await {
                let part = part.context("Scribe stream interrupted")?;
                for byte in part {
                    if byte == b'\n' {
                        let chunk: RuntimeChunk = serde_json::from_slice(&buffer).context("Invalid Scribe event")?;
                        buffer.clear();
                        match chunk {
                            RuntimeChunk::Content { content } => { yield AgentChunk::Content(content); },
                            RuntimeChunk::Completed { .. } => { completed = true; break; },
                            RuntimeChunk::Failed { .. } => { Err::<(), _>(anyhow::anyhow!("Scribe generation failed"))?; },
                        }
                    } else {
                        if buffer.len() >= 65536 { Err::<(), _>(anyhow::anyhow!("Scribe event exceeds limit"))?; }
                        buffer.push(byte);
                    }
                }
                if completed { break; }
            }
            if !completed { Err::<(), _>(anyhow::anyhow!("Scribe stream ended without completion"))?; }
        };
        Ok(Box::pin(stream))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_requires_explicit_terminal_success() -> Result<()> {
        let content: RuntimeChunk =
            serde_json::from_str(r#"{"kind":"content","content":"answer"}"#)?;
        assert!(matches!(content, RuntimeChunk::Content { .. }));
        let failed: RuntimeChunk =
            serde_json::from_str(r#"{"kind":"failed","content":"details"}"#)?;
        assert!(matches!(failed, RuntimeChunk::Failed { .. }));
        assert!(
            serde_json::from_str::<RuntimeChunk>(r#"{"kind":"reasoning"}"#)
                .is_err()
        );
        Ok(())
    }
}
