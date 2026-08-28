use std::io::Cursor;

use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};

use super::*;

struct MockModel {
    events: Vec<ModelStreamEvent>,
    start_error: Option<String>,
    event_delay: Option<Duration>,
}

#[async_trait::async_trait]
impl ChatModel for MockModel {
    async fn stream<'a>(
        &'a self,
        _input: ModelInput,
    ) -> Result<ModelEventStream<'a>> {
        if let Some(error) = &self.start_error {
            return Err(anyhow!(error.clone()));
        }
        if let Some(delay) = self.event_delay {
            let events = self.events.clone().into_iter();
            return Ok(Box::pin(futures::stream::unfold(
                events,
                move |mut events| async move {
                    let event = events.next()?;
                    tokio::time::sleep(delay).await;
                    Some((event, events))
                },
            )));
        }
        Ok(Box::pin(futures::stream::iter(self.events.clone())))
    }
}

fn test_config(max_cached_sessions: usize) -> ScribeConfig {
    ScribeConfig {
        models: vec![model("writer", "mock")],
        default_model: "writer".to_string(),
        system_prompt: "System".to_string(),
        max_seq_len: 64,
        temperature: 0.0,
        max_cached_sessions,
        max_iterations: 2,
    }
}

fn test_engine(
    events: Vec<ModelStreamEvent>,
    start_error: Option<&str>,
    max_cached_sessions: usize,
    completion_tx: mpsc::Sender<ScribeCompletionEvent>,
) -> Result<Arc<ScribeEngine>> {
    let config = test_config(max_cached_sessions);
    let loaded_models = vec![(
        "writer".to_string(),
        Arc::new(MockModel {
            events,
            start_error: start_error.map(str::to_owned),
            event_delay: None,
        }) as Arc<dyn ChatModel>,
    )];
    ScribeEngine::from_loaded_models(
        config,
        completion_tx,
        loaded_models,
        ScribeMetrics::new(1)?,
    )
}

fn request(session_id: &str, message_id: &str) -> ScribeRequest {
    ScribeRequest {
        session_id: session_id.to_string(),
        message_id: message_id.to_string(),
        model_alias: None,
        prompt: "Question".to_string(),
        attachments: Vec::new(),
        grammar_constraint: None,
        database_provider: None,
    }
}

async fn drain_stream(
    mut stream: mpsc::Receiver<Result<ScribeStreamChunk>>,
) -> Result<Vec<ScribeStreamChunk>> {
    let mut chunks = Vec::new();
    while let Some(chunk) = stream.recv().await {
        chunks.push(chunk?);
    }
    Ok(chunks)
}

async fn serve_once(
    status: &str,
    body: Vec<u8>,
    declared_length: Option<usize>,
) -> Result<String> {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let length = declared_length.unwrap_or(body.len());
    let headers = format!(
        "HTTP/1.1 {status}\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n"
    );
    std::mem::drop(tokio::spawn(async move {
        let result: Result<()> = async {
            let (mut stream, _) = listener.accept().await?;
            let mut request = [0_u8; 1024];
            let _bytes_read = stream.read(&mut request).await?;
            stream.write_all(headers.as_bytes()).await?;
            stream.write_all(&body).await?;
            stream.shutdown().await?;
            Ok(())
        }
        .await;
        if let Err(error) = result {
            tracing::error!(%error, "test HTTP server failed");
        }
    }));
    Ok(format!("http://{address}/attachment"))
}

fn model(alias: &str, model_id: &str) -> ScribeModelConfig {
    ScribeModelConfig {
        alias: alias.to_string(),
        model_id: model_id.to_string(),
        model_files: vec![std::path::PathBuf::from("model.uqff")],
        assistant_model_id: None,
        n_predict: None,
    }
}

#[test]
fn model_registry_routes_default_and_named_models() -> Result<()> {
    let registry = ModelRegistry::new(
        "fast",
        vec![("fast".to_string(), 1_u8), ("deep".to_string(), 2_u8)],
    )?;

    assert_eq!(registry.resolve(None)?, &1);
    assert_eq!(registry.resolve(Some("deep"))?, &2);
    assert!(registry.resolve(Some("missing")).is_err());
    assert!(
        ModelRegistry::new("missing", [("fast".to_string(), 1_u8)]).is_err()
    );
    assert!(
        ModelRegistry::new(
            "fast",
            [("fast".to_string(), 1_u8), ("fast".to_string(), 2_u8)]
        )
        .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn engine_commits_notifies_replays_and_unloads_chat() -> Result<()> {
    let (completion_tx, mut completion_rx) = mpsc::channel(8);
    let engine = test_engine(
        vec![
            ModelStreamEvent::Token("<reasoning>Think".to_string()),
            ModelStreamEvent::Ignored,
            ModelStreamEvent::Token("</reasoning>Answer".to_string()),
            ModelStreamEvent::Usage(17),
            ModelStreamEvent::Token(String::new()),
        ],
        None,
        2,
        completion_tx,
    )?;

    let chunks =
        drain_stream(engine.execute_request(request("s1", "m1")).await?)
            .await?;
    assert_eq!(chunks.len(), 2);
    let completion = completion_rx
        .recv()
        .await
        .context("completion event missing")?;
    assert_eq!(completion.status, ScribeCompletionStatus::Completed);
    assert_eq!(completion.session.revision, 1);
    assert_eq!(
        completion.final_content.as_deref(),
        Some("<reasoning>Think</reasoning>Answer")
    );

    let replay =
        drain_stream(engine.execute_request(request("s1", "m1")).await?)
            .await?;
    assert_eq!(replay.len(), 1);
    let replay_completion = completion_rx
        .recv()
        .await
        .context("replay completion event missing")?;
    assert_eq!(replay_completion.session.revision, 1);
    let detached_replay = engine.execute_request(request("s1", "m1")).await?;
    drop(detached_replay);
    completion_rx
        .recv()
        .await
        .context("detached replay completion missing")?;
    assert_eq!(engine.save_conversation("s1").await?.revision, 1);
    assert!(engine.save_conversation("missing").await.is_err());

    let metrics = engine.metrics();
    assert_eq!(metrics.generated_tokens, 17);
    assert_eq!(metrics.completed_messages, 3);
    assert_eq!(metrics.active_sessions, 1);
    assert_eq!(engine.unload_conversation("s1").await?.revision, 1);
    assert_eq!(engine.metrics().active_sessions, 0);
    assert!(engine.unload_conversation("s1").await.is_err());
    Ok(())
}

#[tokio::test]
async fn engine_reports_model_start_and_stream_failures() -> Result<()> {
    let (start_tx, mut start_rx) = mpsc::channel(2);
    let start_engine = test_engine(Vec::new(), Some("offline"), 1, start_tx)?;
    let mut start_stream =
        start_engine.execute_request(request("start", "m1")).await?;
    assert!(matches!(start_stream.recv().await, Some(Err(_))));
    assert!(start_stream.recv().await.is_none());
    assert_eq!(
        start_rx
            .recv()
            .await
            .context("start failure missing")?
            .status,
        ScribeCompletionStatus::Failed
    );

    let detached =
        start_engine.execute_request(request("start", "m2")).await?;
    drop(detached);
    start_rx
        .recv()
        .await
        .context("detached failure completion missing")?;

    let (stream_tx, mut stream_rx) = mpsc::channel(2);
    let stream_engine = test_engine(
        vec![
            ModelStreamEvent::Token("partial".to_string()),
            ModelStreamEvent::Error("broken".to_string()),
        ],
        None,
        1,
        stream_tx,
    )?;
    let mut output = stream_engine
        .execute_request(request("stream", "m2"))
        .await?;
    assert!(matches!(output.recv().await, Some(Ok(_))));
    assert!(matches!(output.recv().await, Some(Err(_))));
    assert_eq!(
        stream_rx
            .recv()
            .await
            .context("stream failure missing")?
            .status,
        ScribeCompletionStatus::Failed
    );
    assert_eq!(stream_engine.metrics().failed_messages, 1);
    assert_eq!(stream_engine.save_conversation("stream").await?.revision, 0);
    Ok(())
}

#[tokio::test]
async fn engine_validates_requests_capacity_and_closed_sessions() -> Result<()>
{
    let (completion_tx, mut completion_rx) = mpsc::channel(4);
    let engine = test_engine(
        vec![
            ModelStreamEvent::Token("answer".to_string()),
            ModelStreamEvent::Token(" continued".to_string()),
        ],
        None,
        1,
        completion_tx,
    )?;
    let mut empty_id = request("s1", " ");
    assert!(engine.execute_request(empty_id).await.is_err());
    empty_id = request("s1", "m1");
    empty_id.prompt = " ".to_string();
    assert!(engine.execute_request(empty_id).await.is_err());
    let mut unknown_model = request("s1", "m1");
    unknown_model.model_alias = Some("missing".to_string());
    assert!(engine.execute_request(unknown_model).await.is_err());
    assert!(engine.load_conversation("").await.is_err());

    let state = engine.session_state("s1").await?;
    assert!(Arc::ptr_eq(&state, &engine.session_state("s1").await?));
    assert!(engine.load_conversation("s2").await.is_err());
    state.close();
    let mut output = engine.execute_request(request("s1", "m1")).await?;
    assert!(matches!(output.recv().await, Some(Err(_))));
    assert_eq!(
        completion_rx
            .recv()
            .await
            .context("closed failure missing")?
            .status,
        ScribeCompletionStatus::Failed
    );
    Ok(())
}

#[tokio::test]
async fn concurrent_session_creation_reuses_inserted_state() -> Result<()> {
    let (completion_tx, _completion_rx) = mpsc::channel(2);
    let engine = test_engine(Vec::new(), None, 64, completion_tx)?;
    for sequence in 0..32 {
        let session_id = format!("race-{sequence}");
        let write_guard = engine.conversations.write().await;
        let first_engine = Arc::clone(&engine);
        let first_id = session_id.clone();
        let first = tokio::spawn(async move {
            first_engine.session_state(&first_id).await
        });
        let second_engine = Arc::clone(&engine);
        let second_id = session_id.clone();
        let second = tokio::spawn(async move {
            second_engine.session_state(&second_id).await
        });
        tokio::task::yield_now().await;
        drop(write_guard);
        let first_state = first.await.context("first task failed")??;
        let second_state = second.await.context("second task failed")??;
        assert!(Arc::ptr_eq(&first_state, &second_state));
    }
    Ok(())
}

#[tokio::test]
async fn detached_completion_receiver_is_counted() -> Result<()> {
    let (completion_tx, completion_rx) = mpsc::channel(1);
    drop(completion_rx);
    let engine = test_engine(
        vec![
            ModelStreamEvent::Token("answer".to_string()),
            ModelStreamEvent::Token(" continued".to_string()),
        ],
        None,
        1,
        completion_tx,
    )?;
    let stream = engine.execute_request(request("s1", "m1")).await?;
    drop(stream);
    for _ in 0..16 {
        if engine.metrics().completion_delivery_failures == 1 {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(engine.metrics().completion_delivery_failures, 1);
    Ok(())
}

#[tokio::test]
async fn engine_hydrates_and_rejects_stale_conflicting_or_closed_state()
-> Result<()> {
    let (completion_tx, _completion_rx) = mpsc::channel(2);
    let engine = test_engine(Vec::new(), None, 4, completion_tx)?;
    let restored = SerializableSession {
        session_id: "fresh".to_string(),
        revision: 1,
        messages: vec![SerializableMessage {
            message_id: None,
            model_alias: None,
            role: MessageRole::System,
            content: "Restored".to_string(),
            attachments: Vec::new(),
        }],
    };
    engine.hydrate_conversation_from_db(&restored).await?;
    assert_eq!(engine.save_conversation("fresh").await?, restored);

    let state = engine.session_state("fresh").await?;
    state.conversation.write().await.revision = 2;
    assert!(
        engine
            .hydrate_conversation_from_db(&restored)
            .await
            .is_err()
    );

    let mut conflicting = restored.clone();
    conflicting.revision = 2;
    conflicting
        .messages
        .first_mut()
        .context("message missing")?
        .content = "Conflict".to_string();
    assert!(
        engine
            .hydrate_conversation_from_db(&conflicting)
            .await
            .is_err()
    );

    let closed = SerializableSession {
        session_id: "closed".to_string(),
        revision: 0,
        messages: Vec::new(),
    };
    engine.session_state("closed").await?.close();
    assert!(engine.hydrate_conversation_from_db(&closed).await.is_err());
    Ok(())
}

#[tokio::test]
async fn engine_covers_default_ids_conflicts_attachments_and_overflow()
-> Result<()> {
    let (completion_tx, mut completion_rx) = mpsc::channel(8);
    let engine = test_engine(
        vec![ModelStreamEvent::Token("answer".to_string())],
        None,
        8,
        completion_tx,
    )?;
    let output = engine
        .execute_agent_step("default", "Question", Vec::new(), None)
        .await?;
    assert_eq!(drain_stream(output).await?.len(), 1);
    let generated =
        completion_rx.recv().await.context("completion missing")?;
    assert!(generated.message_id.starts_with("default-"));

    let output = engine.execute_request(request("conflict", "same")).await?;
    assert_eq!(drain_stream(output).await?.len(), 1);
    completion_rx.recv().await.context("completion missing")?;
    let mut conflict = request("conflict", "same");
    conflict.prompt = "Different".to_string();
    let mut conflict_output = engine.execute_request(conflict).await?;
    assert!(matches!(conflict_output.recv().await, Some(Err(_))));
    assert_eq!(
        completion_rx
            .recv()
            .await
            .context("conflict missing")?
            .status,
        ScribeCompletionStatus::Failed
    );

    let mut attachment = request("attachment", "m1");
    attachment.attachments = vec![AttachmentUrl::Audio(
        "http://127.0.0.1:1/missing".to_string(),
    )];
    let mut attachment_output = engine.execute_request(attachment).await?;
    assert!(matches!(attachment_output.recv().await, Some(Err(_))));
    completion_rx
        .recv()
        .await
        .context("attachment failure missing")?;

    let audio_url = serve_once("200 OK", vec![1, 2], None).await?;
    let mut audio = request("audio", "m1");
    audio.attachments = vec![AttachmentUrl::Audio(audio_url)];
    drain_stream(engine.execute_request(audio).await?).await?;
    completion_rx
        .recv()
        .await
        .context("audio completion missing")?;

    let mut png = Cursor::new(Vec::new());
    image::DynamicImage::new_rgb8(1, 1)
        .write_to(&mut png, image::ImageFormat::Png)?;
    let image_url = serve_once("200 OK", png.into_inner(), None).await?;
    let mut image = request("image", "m1");
    image.attachments = vec![AttachmentUrl::Image(image_url)];
    drain_stream(engine.execute_request(image).await?).await?;
    completion_rx
        .recv()
        .await
        .context("image completion missing")?;

    let overflow_state = engine.session_state("overflow").await?;
    overflow_state.conversation.write().await.revision = u64::MAX;
    let mut overflow_output =
        engine.execute_request(request("overflow", "m1")).await?;
    assert!(matches!(overflow_output.recv().await, Some(Ok(_))));
    assert!(matches!(overflow_output.recv().await, Some(Err(_))));
    assert_eq!(
        completion_rx
            .recv()
            .await
            .context("overflow missing")?
            .status,
        ScribeCompletionStatus::Failed
    );
    Ok(())
}

#[tokio::test]
async fn parser_flushes_partial_tag_through_generation() -> Result<()> {
    let (connected_tx, mut connected_rx) = mpsc::channel(2);
    let connected_engine = test_engine(
        vec![ModelStreamEvent::Token("partial <reas".to_string())],
        None,
        1,
        connected_tx,
    )?;
    let connected_chunks = drain_stream(
        connected_engine
            .execute_request(request("connected", "m"))
            .await?,
    )
    .await?;
    assert_eq!(connected_chunks.len(), 2);
    connected_rx.recv().await.context("completion missing")?;

    let (completion_tx, mut completion_rx) = mpsc::channel(2);
    let engine = ScribeEngine::from_loaded_models(
        test_config(1),
        completion_tx,
        vec![(
            "writer".to_string(),
            Arc::new(MockModel {
                events: vec![
                    ModelStreamEvent::Token("partial <reas".to_string()),
                    ModelStreamEvent::Ignored,
                ],
                start_error: None,
                event_delay: Some(Duration::from_millis(5)),
            }) as Arc<dyn ChatModel>,
        )],
        ScribeMetrics::new(1)?,
    )?;
    let mut chunks = engine.execute_request(request("s", "m")).await?;
    assert!(matches!(chunks.recv().await, Some(Ok(_))));
    drop(chunks);
    completion_rx.recv().await.context("completion missing")?;
    Ok(())
}

#[test]
fn config_rejects_duplicate_model_aliases() -> Result<()> {
    let mut config = ScribeConfig::new()?;
    config.models =
        vec![model("writer", "model-a"), model("writer", "model-b")];
    config.default_model = "writer".to_string();

    assert!(config.validate().is_err());
    Ok(())
}

#[test]
fn config_validation_covers_every_invalid_resource_bound() -> Result<()> {
    let mut config = test_config(1);
    config.models.clear();
    assert!(config.validate().is_err());

    config = test_config(0);
    assert!(config.validate().is_err());
    config = test_config(1);
    config.max_seq_len = 0;
    assert!(config.validate().is_err());
    config = test_config(1);
    config
        .models
        .first_mut()
        .context("model missing")?
        .alias
        .clear();
    assert!(config.validate().is_err());
    config = test_config(1);
    config
        .models
        .first_mut()
        .context("model missing")?
        .model_id
        .clear();
    assert!(config.validate().is_err());
    config = test_config(1);
    config
        .models
        .first_mut()
        .context("model missing")?
        .model_files
        .clear();
    assert!(config.validate().is_err());
    config = test_config(1);
    config.default_model = "missing".to_string();
    assert!(config.validate().is_err());
    Ok(())
}

#[tokio::test]
async fn attachment_resolver_covers_image_and_audio_outcomes() -> Result<()> {
    let client = reqwest::Client::new();
    let mut png = Cursor::new(Vec::new());
    image::DynamicImage::new_rgb8(1, 1)
        .write_to(&mut png, image::ImageFormat::Png)?;
    let image_url = serve_once("200 OK", png.into_inner(), None).await?;
    let image =
        S3AttachmentResolver::resolve_image(&client, &image_url).await?;
    assert_eq!(image.width(), 1);

    let bad_image_url =
        serve_once("200 OK", b"invalid".to_vec(), None).await?;
    assert!(
        S3AttachmentResolver::resolve_image(&client, &bad_image_url)
            .await
            .is_err()
    );
    let image_status_url =
        serve_once("404 Not Found", Vec::new(), None).await?;
    assert!(
        S3AttachmentResolver::resolve_image(&client, &image_status_url)
            .await
            .is_err()
    );
    let truncated_image_url = serve_once("200 OK", vec![1], Some(4)).await?;
    assert!(
        S3AttachmentResolver::resolve_image(&client, &truncated_image_url)
            .await
            .is_err()
    );
    assert!(
        S3AttachmentResolver::resolve_image(&client, "http://127.0.0.1:1")
            .await
            .is_err()
    );

    let audio_url = serve_once("200 OK", vec![1, 2, 3], None).await?;
    let resolved = S3AttachmentResolver::resolve_all(
        &client,
        &[AttachmentUrl::Audio(audio_url)],
    )
    .await?;
    assert!(
        matches!(resolved.first(), Some(Attachment::Audio(bytes)) if bytes == &[1, 2, 3])
    );
    let empty_url = serve_once("200 OK", Vec::new(), None).await?;
    assert!(
        S3AttachmentResolver::resolve_all(
            &client,
            &[AttachmentUrl::Audio(empty_url)]
        )
        .await
        .is_err()
    );
    let audio_status_url = serve_once("500 Error", Vec::new(), None).await?;
    assert!(
        S3AttachmentResolver::resolve_all(
            &client,
            &[AttachmentUrl::Audio(audio_status_url)]
        )
        .await
        .is_err()
    );
    let truncated_url = serve_once("200 OK", vec![1], Some(4)).await?;
    assert!(
        S3AttachmentResolver::resolve_all(
            &client,
            &[AttachmentUrl::Audio(truncated_url)]
        )
        .await
        .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn conversation_hydrates_text_and_image_messages() -> Result<()> {
    let mut png = Cursor::new(Vec::new());
    image::DynamicImage::new_rgb8(1, 1)
        .write_to(&mut png, image::ImageFormat::Png)?;
    let image_url = serve_once("200 OK", png.into_inner(), None).await?;
    let audio_url = serve_once("200 OK", vec![1, 2], None).await?;
    let session = SerializableSession {
        session_id: "hydrated".to_string(),
        revision: 3,
        messages: vec![
            SerializableMessage {
                message_id: None,
                model_alias: None,
                role: MessageRole::System,
                content: "System".to_string(),
                attachments: Vec::new(),
            },
            SerializableMessage {
                message_id: Some("m1".to_string()),
                model_alias: Some("writer".to_string()),
                role: MessageRole::User,
                content: "Image".to_string(),
                attachments: vec![AttachmentUrl::Image(image_url)],
            },
            SerializableMessage {
                message_id: Some("m2".to_string()),
                model_alias: Some("writer".to_string()),
                role: MessageRole::Assistant,
                content: "Audio fallback".to_string(),
                attachments: vec![AttachmentUrl::Audio(audio_url)],
            },
        ],
    };
    let conversation =
        Conversation::from_serializable(&session, &reqwest::Client::new())
            .await?;
    assert_eq!(conversation.revision(), 3);
    assert_eq!(conversation.snapshot("hydrated"), session);
    Ok(())
}

#[test]
fn completed_turn_is_committed_atomically_and_idempotently() -> Result<()> {
    let mut conversation = Conversation::new("System prompt");
    let turn = CompletedTurn {
        message_id: "message-1",
        model_alias: "writer",
        prompt: "Question",
        attachments: Vec::new(),
        response: "Answer",
    };

    conversation.commit_turn(&turn)?;
    assert_eq!(conversation.revision(), 1);
    assert_eq!(conversation.serializable_history.len(), 3);
    let cached = conversation
        .cached_turn("message-1")
        .context("completed turn was not cached")?;
    assert_eq!(cached.model_alias, "writer");
    assert_eq!(cached.prompt, "Question");
    assert_eq!(cached.response, "Answer");
    assert!(cached.matches_request("writer", "Question", &[]));
    assert!(!cached.matches_request("writer", "Different", &[]));
    assert!(conversation.commit_turn(&turn).is_err());
    assert_eq!(conversation.revision(), 1);
    assert_eq!(conversation.serializable_history.len(), 3);
    Ok(())
}

#[test]
fn serialized_session_accepts_legacy_payload_without_revision() -> Result<()> {
    let session: SerializableSession =
        serde_json::from_str(r#"{"session_id":"legacy","messages":[]}"#)?;

    assert_eq!(session.revision, 0);
    Ok(())
}

#[test]
fn test_message_role_conversion() {
    assert_eq!(
        TextMessageRole::from(MessageRole::System),
        TextMessageRole::System
    );
    assert_eq!(
        TextMessageRole::from(MessageRole::User),
        TextMessageRole::User
    );
    assert_eq!(
        TextMessageRole::from(MessageRole::Assistant),
        TextMessageRole::Assistant
    );
}

#[test]
fn test_conversation_new() -> Result<()> {
    let conv = Conversation::new("System prompt");
    assert_eq!(conv.serializable_history.len(), 1);
    assert_eq!(
        conv.serializable_history
            .first()
            .context("system message missing")?
            .role,
        MessageRole::System
    );

    let conv_empty = Conversation::new("");
    assert_eq!(conv_empty.serializable_history.len(), 0);
    Ok(())
}

#[test]
fn test_scribe_config_defaults() -> Result<()> {
    let config = ScribeConfig::new()?;
    assert_eq!(config.max_seq_len, 4096);
    assert_eq!(config.temperature, 0.1);
    assert_eq!(config.max_cached_sessions, 1000);
    assert_eq!(config.max_iterations, 10);
    assert_eq!(config.models.len(), 1);
    let preset = ScribeModelPreset::Writer;
    assert_eq!(config.default_model, preset.alias());
    assert_eq!(config.models.as_slice(), [preset.model_config()]);
    config.validate()?;
    Ok(())
}

#[test]
fn test_parser_pure_content() -> Result<()> {
    let mut parser = TokenStreamParser::new();
    let mut output = Vec::new();
    parser.advance("Hello ", &mut output);
    parser.advance("world!", &mut output);

    assert_eq!(output.len(), 2);
    assert!(matches!(
        output.first(),
        Some(ScribeStreamChunk::Content(content)) if content == "Hello "
    ));
    assert!(matches!(
        output.get(1),
        Some(ScribeStreamChunk::Content(content)) if content == "world!"
    ));
    assert!(parser.flush().is_none());

    let mut unicode_parser = TokenStreamParser::new();
    let mut unicode_output = Vec::new();
    unicode_parser.advance(
        "Café <reasoning>réfléchir</reasoning>réponse",
        &mut unicode_output,
    );
    assert_eq!(unicode_output.len(), 3);
    assert!(matches!(
        unicode_output.first(),
        Some(ScribeStreamChunk::Content(content)) if content == "Café "
    ));
    Ok(())
}

#[test]
fn test_parser_flush_partial_match() -> Result<()> {
    let mut parser = TokenStreamParser::new();
    let mut output = Vec::new();
    parser.advance("Hello <reas", &mut output);

    assert_eq!(output.len(), 1);
    assert!(matches!(
        output.first(),
        Some(ScribeStreamChunk::Content(content)) if content == "Hello "
    ));
    assert!(matches!(
        parser.flush(),
        Some(ScribeStreamChunk::Content(content)) if content == "<reas"
    ));
    Ok(())
}

#[test]
fn test_parser_with_reasoning() -> Result<()> {
    let mut parser = TokenStreamParser::new();
    let mut output = Vec::new();
    parser.advance("<reasoning>Thinking</reasoning>Done", &mut output);

    assert_eq!(output.len(), 2);
    assert!(matches!(
        output.first(),
        Some(ScribeStreamChunk::Reasoning(reasoning)) if reasoning == "Thinking"
    ));
    assert!(matches!(
        output.get(1),
        Some(ScribeStreamChunk::Content(content)) if content == "Done"
    ));

    let mut prefixed = TokenStreamParser::new();
    let mut prefixed_output = Vec::new();
    prefixed.advance(
        "Before<reasoning>Thinking</reasoning>After",
        &mut prefixed_output,
    );
    assert_eq!(prefixed_output.len(), 3);

    let mut unfinished = TokenStreamParser::new();
    let mut unfinished_output = Vec::new();
    unfinished.advance("<reasoning>unfinished</reas", &mut unfinished_output);
    assert_eq!(unfinished_output.len(), 1);
    assert!(matches!(
        unfinished.flush(),
        Some(ScribeStreamChunk::Reasoning(reasoning)) if reasoning == "</reas"
    ));
    Ok(())
}

#[test]
fn closing_session_prevents_queued_generation() {
    let session = SessionState::new("");
    assert!(session.is_accepting_requests());
    session.close();
    assert!(!session.is_accepting_requests());
}

#[cfg(feature = "latex")]
#[test]
fn test_latex_escape() -> Result<()> {
    use tera::to_value;
    let val = to_value("Context & Percent %")?;
    let escaped = report::ScribeReport::latex_escape(
        &val,
        &std::collections::HashMap::new(),
    )?;
    assert_eq!(
        escaped.as_str().context("escaped value is not a string")?,
        "Context \\& Percent \\%"
    );
    Ok(())
}
