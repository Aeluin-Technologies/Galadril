use std::io::Write as _;

use anyhow::{Context, Result};
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
use scribe::engine::{
    ScribeConfig, ScribeEngine, ScribeRequest, ScribeStreamChunk,
};

#[tokio::main]
async fn main() -> Result<()> {
    let telemetry = TelemetryConfig::Binary {
        name: "galadril-scribe-example",
        version: env!("CARGO_PKG_VERSION"),
    }
    .configure()?;
    let mut config = ScribeConfig::new()?;
    config.max_seq_len = 4096;
    config.temperature = 0.0;
    let model_alias = config.default_model.clone();
    let (completion_tx, mut completion_rx) = tokio::sync::mpsc::channel(100);
    let engine = ScribeEngine::new(config, completion_tx).await?;

    let mut reply_stream = engine
        .execute_request(ScribeRequest {
            session_id: "session-1".to_string(),
            message_id: "message-1".to_string(),
            model_alias: Some(model_alias),
            prompt: "What verified supply-chain data is available?"
                .to_string(),
            attachments: Vec::new(),
            grammar_constraint: None,
            database_provider: None,
        })
        .await?;
    let stdout = std::io::stdout();
    while let Some(chunk_result) = reply_stream.recv().await {
        let chunk = chunk_result?;
        match chunk {
            ScribeStreamChunk::Reasoning(tokens) => {
                print!("{tokens}");
            },
            ScribeStreamChunk::Content(tokens) => {
                print!("{tokens}");
            },
        }
        stdout.lock().flush()?;
    }

    let completion = completion_rx.recv().await.context(
        "completion channel closed before persistence notification",
    )?;
    tracing::info!(
        event.name = "scribe.example.persistence.ready",
        session.id = %completion.session.session_id,
        session.revision = completion.session.revision,
        message.id = %completion.message_id,
        status = ?completion.status,
        "complete chat snapshot is ready to persist"
    );
    tracing::info!(metrics = ?engine.metrics(), "Scribe metrics snapshot");
    drop(engine);
    telemetry.shutdown()
}
