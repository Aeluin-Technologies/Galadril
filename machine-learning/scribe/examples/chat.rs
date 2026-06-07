use std::io::Write;

use scribe::engine::{ScribeConfig, ScribeEngine, ScribeStreamChunk};
use tracing_subscriber::prelude::*;
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() {
    tracing_subscriber::registry()
        .with(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                EnvFilter::new("scribe=info,mistralrs=info")
            }),
        )
        .with(fmt::layer())
        .init();

    let mut config = ScribeConfig::new()
        .expect("Cannot generate configuration mapping payload");
    config.max_seq_len = 4096;
    config.temperature = 0.0;

    let (persistence_tx, mut persistence_rx) = tokio::sync::mpsc::channel(100);

    let engine = ScribeEngine::new(config, persistence_tx).await.expect(
        "Cannot initialize backend Scribe engine context infrastructure",
    );

    let session_id = "session-1";
    tokio::spawn(async move {
        tracing::info!("Database background persistence listener task active");
        while let Some(msg) = persistence_rx.recv().await {
            println!("Session: {}", msg.session_id);
            println!("Payload: {} characters", msg.final_content.len());
            println!("Assets: {} URLs", msg.runtime_attachments.len());
        }
    });

    let prompt1 = r#"
    Who are you? Are you an agent dedicated for my complex supply chain? What is currently on the database?
    "#;

    println!("User:\n{prompt1}");

    let mut reply_stream = engine
        .execute_agent_step(session_id, prompt1, Vec::new(), None)
        .await
        .expect("Failed to open communication channel stream pipeline");

    let stdout = std::io::stdout();
    let mut current_block = None;

    while let Some(chunk_result) = reply_stream.recv().await {
        let chunk = chunk_result
            .expect("Error received during model generation context");
        let mut handle = stdout.lock();

        match chunk {
            ScribeStreamChunk::Reasoning(tokens) => {
                if current_block != Some("reasoning") {
                    print!("\n  [Thinking] ");
                    current_block = Some("reasoning");
                }
                print!("{tokens}");
            },
            ScribeStreamChunk::Content(tokens) => {
                if current_block != Some("content") {
                    print!("\n   [Assistant] ");
                    current_block = Some("content");
                }
                print!("{tokens}");
            },
        }
        let _ = handle.flush();
    }

    drop(reply_stream);

    println!("\n");

    let prompt2 = r#"
    Calculate the following expression: (sqrt(144) * 15) ^ 3 / 2.5 + 47.89. What percentage of the Earth's circumference does this result represent?
    "#;

    println!("User:\n{prompt2}");

    let mut reply_stream2 = engine
        .execute_agent_step(session_id, prompt2, Vec::new(), None)
        .await
        .expect("Failed to open communication channel stream pipeline");

    current_block = None;

    while let Some(chunk_result) = reply_stream2.recv().await {
        let chunk = chunk_result
            .expect("Error received during model generation context");
        let mut handle = stdout.lock();

        match chunk {
            ScribeStreamChunk::Reasoning(tokens) => {
                if current_block != Some("reasoning") {
                    print!("\n  [Thinking] ");
                    current_block = Some("reasoning");
                }
                print!("{tokens}");
            },
            ScribeStreamChunk::Content(tokens) => {
                if current_block != Some("content") {
                    print!("\n   [Assistant] ");
                    current_block = Some("content");
                }
                print!("{tokens}");
            },
        }
        let _ = handle.flush();
    }

    // Explicitly close the local receiver hook and allow final flush signals
    // to resolve.
    drop(reply_stream2);

    tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
}
