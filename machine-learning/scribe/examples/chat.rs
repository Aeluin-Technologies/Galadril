use scribe::{ScribeChat, ScribeConfig};
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() {
    tracing_subscriber::registry()
        .with(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with(fmt::layer())
        .init();

    let config = ScribeConfig::new()
        .expect("cannot generate config")
        .with_max_seq_len(4096);

    let mut chat = ScribeChat::new(&config)
        .await
        .expect("cannot initialize chat engine");

    let prompt1 = "Hello! Can you briefly explain what a DSGE model is in macroeconomics?";
    println!("User: {prompt1}");

    let reply1 = chat.chat(prompt1).await.expect("Failed to get response");
    println!("Assistant: {reply1}\n");

    let prompt2 =
        "What are the main alternatives to the model you just described?";
    println!("User: {prompt2}");

    let reply2 = chat.chat(prompt2).await.expect("Failed to get response");
    println!("Assistant: {reply2}\n");
}
