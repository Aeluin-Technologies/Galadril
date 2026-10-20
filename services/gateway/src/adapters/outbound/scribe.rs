//! Private HTTP agent adapter; Gateway owns persistence and delegated
//! authority.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use futures::StreamExt as _;
use serde::Deserialize;
use serde_json::json;

use crate::application::ports::conversation_agent::{
    AgentChunk, AgentModelCatalog, AgentRequest, AgentStream,
    ConversationAgent,
};
use crate::application::usecases::chat_tools::ChatTools;
use crate::config::ScribeRuntimeConfig;

pub struct ScribeAgent {
    client: reqwest::Client,
    endpoint: String,
    models_endpoint: String,
    tools: Arc<ChatTools>,
}

pub struct DisabledScribeAgent;

#[async_trait::async_trait]
impl ConversationAgent for DisabledScribeAgent {
    async fn models(&self) -> Result<AgentModelCatalog> {
        Ok(AgentModelCatalog::default())
    }

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
    Activity {
        content: String,
    },
    Tool {
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
            models_endpoint: format!(
                "{}/models",
                endpoint.as_str().trim_end_matches('/')
            ),
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
    #[tracing::instrument(skip_all)]
    async fn models_for(&self, owner: &str) -> Result<AgentModelCatalog> {
        anyhow::ensure!(
            owner.len() == 64 &&
                owner.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "Invalid provider account owner"
        );
        fetch_models(
            &self.client,
            &format!("{}?owner={owner}", self.models_endpoint),
        )
        .await
    }

    #[tracing::instrument(skip_all)]
    async fn chatgpt_operation(
        &self,
        request: serde_json::Value,
    ) -> Result<serde_json::Value> {
        let endpoint = self
            .endpoint
            .strip_suffix("/runs")
            .context("Invalid Scribe route")?;
        let mut response = self
            .client
            .post(format!("{endpoint}/openai/accounts"))
            .json(&request)
            .timeout(Duration::from_secs(35))
            .send()
            .await
            .context("ChatGPT account transport unavailable")?;
        anyhow::ensure!(
            response.status().is_success(),
            "ChatGPT account operation unavailable"
        );
        let mut body = Vec::with_capacity(4096);
        while let Some(part) = response
            .chunk()
            .await
            .context("ChatGPT account response interrupted")?
        {
            anyhow::ensure!(
                body.len().saturating_add(part.len()) <= 65536,
                "Account response exceeds limit"
            );
            body.extend_from_slice(&part);
        }
        serde_json::from_slice(&body)
            .context("Invalid ChatGPT account response")
    }

    #[tracing::instrument(skip_all)]
    async fn document_operation(
        &self,
        operation: crate::application::ports::conversation_agent::DocumentOperation,
        request: serde_json::Value,
    ) -> Result<serde_json::Value> {
        let url = format!(
            "{}/documents/{}",
            self.endpoint.trim_end_matches("/runs"),
            operation.path()
        );
        let response = self
            .client
            .post(url)
            .timeout(Duration::from_secs(180))
            .json(&request)
            .send()
            .await?;
        anyhow::ensure!(
            response.status().is_success(),
            "Document inference unavailable"
        );
        let mut response = response.bytes_stream();
        let mut body = Vec::new();
        while let Some(chunk) = response.next().await {
            let chunk = chunk?;
            anyhow::ensure!(
                body.len() + chunk.len() <= 8_388_608,
                "Document inference response exceeds limit"
            );
            body.extend_from_slice(&chunk);
        }
        Ok(serde_json::from_slice(&body)?)
    }

    async fn capabilities(
        &self,
    ) -> Result<crate::domain::capabilities::CapabilityCatalog> {
        let base = self
            .models_endpoint
            .strip_suffix("/models")
            .context("Invalid Scribe catalog route")?;
        let bytes = fetch_catalog_body(
            &self.client,
            &format!("{base}/capabilities"),
            262144,
        )
        .await?;
        parse_scribe_catalog(&bytes)
    }

    async fn submit_generation(
        &self,
        request: serde_json::Value,
    ) -> Result<()> {
        let endpoint = self
            .endpoint
            .strip_suffix("/runs")
            .context("Invalid Scribe route")?;
        let response = self
            .client
            .post(format!("{endpoint}/generations"))
            .json(&request)
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .context("Scribe submission unavailable")?;
        anyhow::ensure!(
            response.status() == reqwest::StatusCode::ACCEPTED,
            "Scribe refused background generation"
        );
        Ok(())
    }

    async fn answer_generation(
        &self,
        id: &str,
        capability: &str,
        answer: serde_json::Value,
    ) -> Result<()> {
        anyhow::ensure!(
            id.len() == 32 && id.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "Invalid generation identifier"
        );
        anyhow::ensure!(
            serde_json::to_vec(&answer)?.len() <= 65536,
            "Answer exceeds limit"
        );
        let endpoint = self
            .endpoint
            .strip_suffix("/runs")
            .context("Invalid Scribe route")?;
        let response = self
            .client
            .post(format!("{endpoint}/generations/{id}/answers"))
            .bearer_auth(capability)
            .json(&answer)
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .context("Scribe interaction unavailable")?;
        anyhow::ensure!(
            response.status().is_success(),
            "Scribe response rejected"
        );
        Ok(())
    }

    async fn cancel_generation(
        &self,
        id: &str,
        capability: &str,
    ) -> Result<()> {
        anyhow::ensure!(
            id.len() == 32 && id.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "Invalid generation identifier"
        );
        let endpoint = self
            .endpoint
            .strip_suffix("/runs")
            .context("Invalid Scribe route")?;
        let response = self
            .client
            .post(format!("{endpoint}/generations/{id}/cancel"))
            .bearer_auth(capability)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .context("Scribe cancellation unavailable")?;
        anyhow::ensure!(
            response.status().is_success() ||
                response.status() == reqwest::StatusCode::NOT_FOUND,
            "Scribe cancellation refused"
        );
        Ok(())
    }

    async fn models(&self) -> Result<AgentModelCatalog> {
        fetch_models(&self.client, &self.models_endpoint).await
    }

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
            .json(&json!({"prompt": request.prompt, "model_alias": request.model_alias,
                "reasoning_effort": request.reasoning_effort,
                "history": history, "attachments": Self::attachments(&request.attachments),
                "capability": lease.token()}))
            .send().await.context("Scribe transport failed")?;
        anyhow::ensure!(
            response.status().is_success(),
            "Scribe refused generation"
        );
        let mut decoded = decode_stream(
            response
                .bytes_stream()
                .map(|part| part.context("Scribe stream interrupted")),
        );
        let stream = async_stream::try_stream! {
            let _lease = lease;
            while let Some(chunk) = decoded.next().await {
                yield chunk?;
            }
        };
        Ok(Box::pin(stream))
    }
}

fn parse_scribe_catalog(
    bytes: &[u8],
) -> Result<crate::domain::capabilities::CapabilityCatalog> {
    let catalog =
        crate::domain::capabilities::CapabilityCatalog::parse(bytes)?;
    anyhow::ensure!(
        catalog.capabilities.iter().all(|item| item
            .targets
            .contains(&crate::domain::capabilities::CapabilityTarget::Scribe)),
        "Invalid Scribe capability target"
    );
    Ok(catalog)
}

async fn fetch_catalog_body(
    client: &reqwest::Client,
    endpoint: &str,
    max_bytes: usize,
) -> Result<Vec<u8>> {
    let mut response = client
        .get(endpoint)
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .context("Scribe catalog transport failed")?;
    anyhow::ensure!(
        response.status().is_success(),
        "Scribe catalog unavailable"
    );
    let mut body = Vec::with_capacity(4096);
    while let Some(chunk) = response
        .chunk()
        .await
        .context("Scribe catalog interrupted")?
    {
        anyhow::ensure!(
            body.len().saturating_add(chunk.len()) <= max_bytes,
            "Scribe catalog exceeds limit"
        );
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

/// Bounds catalog reads independently from the long-lived generation timeout.
async fn fetch_models(
    client: &reqwest::Client,
    endpoint: &str,
) -> Result<AgentModelCatalog> {
    let body = fetch_catalog_body(client, endpoint, 65536).await?;
    let catalog: AgentModelCatalog = serde_json::from_slice(&body)
        .map_err(|_| anyhow::anyhow!("Invalid Scribe catalog"))?;
    anyhow::ensure!(
        catalog.models.len() <= 64,
        "Scribe model count exceeds limit"
    );
    Ok(catalog)
}

/// Bounds individual frames while retaining the transport's zero-copy chunks.
fn decode_stream<B, S>(input: S) -> AgentStream
where
    B: AsRef<[u8]> + Send + 'static,
    S: futures::Stream<Item = Result<B>> + Send + 'static,
{
    Box::pin(async_stream::try_stream! {
        let mut input = Box::pin(input);
        let mut buffer = Vec::with_capacity(8192);
        let mut completed = false;
        while let Some(part) = input.next().await {
            let part = part?;
            for &byte in part.as_ref() {
                if byte == b'\n' {
                    let chunk: RuntimeChunk = serde_json::from_slice(&buffer)
                        .map_err(|_| anyhow::anyhow!("Invalid Scribe event"))?;
                    buffer.clear();
                    match chunk {
                        RuntimeChunk::Content { content } => { yield AgentChunk::Content(content); },
                        RuntimeChunk::Activity { content } => { crate::domain::agent_activity::AgentActivity::parse(&content)?; },
                        RuntimeChunk::Tool { content } => {
                            let progress = crate::domain::pengolo::OutputEvent {sequence: 1, kind: "tool".into(), content};
                            progress.validate_tool_progress()?;
                        },
                        RuntimeChunk::Completed { .. } => { completed = true; break; },
                        RuntimeChunk::Failed { .. } => {
                            Err::<(), _>(anyhow::anyhow!("Scribe generation failed"))?;
                        },
                    }
                } else {
                    if buffer.len() >= 65536 {
                        Err::<(), _>(anyhow::anyhow!("Scribe event exceeds limit"))?;
                    }
                    buffer.push(byte);
                }
            }
            if completed { break; }
        }
        if !completed {
            Err::<(), _>(anyhow::anyhow!("Scribe stream ended without completion"))?;
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capabilities_private_contract_rejects_pipeline_only_entries()
    -> Result<()> {
        let pipeline = include_bytes!(
            "../../../../../platform/pipeline/src/galadril_pipeline/builtin_capabilities.json"
        );
        assert!(parse_scribe_catalog(pipeline).is_err());
        let valid = json!({"schema_version":1,"capabilities":[{"id":"law.algorithm.timeline","plugin_id":"law","plugin_version":"1.0.0","kind":"algorithm","targets":["scribe"],"name":"Timeline","description":"Analyze chronology","parameters_schema":{"type":"object"}}]});
        let catalog = parse_scribe_catalog(&serde_json::to_vec(&valid)?)?;
        assert_eq!(catalog.capabilities.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn catalog_transport_bounds_and_validates_responses() -> Result<()> {
        let app = axum::Router::new().route(
            "/models",
            axum::routing::get(|| async {
                axum::Json(json!({
                    "default_model": "cloud",
                    "models": [{
                        "alias": "cloud", "provider": "openai", "model": "gpt-5.1",
                        "reasoning_efforts": ["low", "high"],
                        "default_reasoning_effort": "high",
                    }],
                }))
            }),
        )
        .route("/oversized", axum::routing::get(|| async { "x".repeat(65537) }))
        .route("/invalid", axum::routing::get(|| async { "private-provider-details" }))
        .route("/denied", axum::routing::get(|| async { axum::http::StatusCode::FORBIDDEN }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = format!("http://{}/models", listener.local_addr()?);
        let (stop_tx, stop_rx) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(async move {
                    if stop_rx.await.is_err() {
                        tracing::debug!(
                            "catalog test shutdown sender dropped"
                        );
                    }
                })
                .await
        });
        let catalog = fetch_models(&reqwest::Client::new(), &endpoint).await?;
        assert_eq!(catalog.default_model.as_deref(), Some("cloud"));
        assert_eq!(catalog.models.len(), 1);
        assert!(catalog.models.first().is_some_and(|model| {
            model.provider == "openai" &&
                model.reasoning_efforts == ["low", "high"]
        }));
        for path in ["oversized", "invalid", "denied"] {
            let invalid = endpoint.replace("/models", &format!("/{path}"));
            let result = fetch_models(&reqwest::Client::new(), &invalid).await;
            assert!(result.is_err());
            assert!(
                !format!("{result:?}").contains("private-provider-details")
            );
        }
        stop_tx
            .send(())
            .map_err(|()| anyhow::anyhow!("shutdown failed"))?;
        server.await??;
        Ok(())
    }

    #[tokio::test]
    async fn interactive_progress_does_not_terminate_legacy_text_streams()
    -> Result<()> {
        let frames = [
            json!({"kind":"content","content":"Initial "}),
            json!({"kind":"activity","content":serde_json::to_string(&json!({"type":"phase","phase":"thinking","agent_id":null}))?}),
            json!({"kind":"tool","content":"search"}),
            json!({"kind":"content","content":"final"}),
            json!({"kind":"completed"}),
        ];
        let mut bytes = Vec::new();
        for frame in frames {
            serde_json::to_writer(&mut bytes, &frame)?;
            bytes.push(b'\n');
        }
        let input = [Ok(bytes)];
        let chunks = decode_stream(futures::stream::iter(input))
            .collect::<Vec<_>>()
            .await;
        assert_eq!(chunks.len(), 2);
        for chunk in chunks {
            assert!(chunk.is_ok());
        }
        Ok(())
    }

    #[tokio::test]
    async fn disabled_scribe_has_an_empty_catalog() -> Result<()> {
        let catalog = DisabledScribeAgent.models().await?;
        assert!(catalog.default_model.is_none());
        assert!(catalog.models.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn stream_decoder_handles_fragmentation_and_rejects_truncation()
    -> Result<()> {
        let parts = [
            b"{\"kind\":\"content\",\"content\":\"answer\"}\n{\"kind\":"
                .to_vec(),
            b"\"completed\"}\n".to_vec(),
        ];
        let stream = futures::stream::iter(parts.into_iter().map(Ok));
        let output = decode_stream(stream).collect::<Vec<_>>().await;
        assert_eq!(output.len(), 1);
        assert!(
            matches!(output.first(), Some(Ok(AgentChunk::Content(content))) if content == "answer")
        );
        let incomplete = futures::stream::iter([Ok(
            b"{\"kind\":\"content\",\"content\":\"answer\"}\n".to_vec(),
        )]);
        let output = decode_stream(incomplete).collect::<Vec<_>>().await;
        assert!(output.last().is_some_and(Result::is_err));
        let oversized = futures::stream::iter([Ok(vec![b'x'; 65537])]);
        let output = decode_stream(oversized).collect::<Vec<_>>().await;
        assert!(output.first().is_some_and(Result::is_err));
        Ok(())
    }

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
