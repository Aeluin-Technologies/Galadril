//! Custom engine of Scribe.
//! Use any model you want.

use std::collections::HashMap;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use futures::{Stream, StreamExt as _};
use mistralrs::{
    ChatCompletionChunkResponse, Constraint, IsqBits, LlguidanceGrammar,
    Model, MultimodalMessages, PagedAttentionMetaBuilder, RequestBuilder,
    Response, TextMessageRole, UqffMultimodalModelBuilder,
};
use serde::Deserialize;
use tokio::sync::{Mutex, RwLock, mpsc};
use tracing::Instrument as _;

pub use crate::config::{ScribeConfig, ScribeModelConfig, ScribeModelPreset};
use crate::session::CompletedTurn;
pub use crate::session::{
    Attachment, AttachmentUrl, Conversation, MessageRole,
    S3AttachmentResolver, ScribeCompletionEvent, ScribeCompletionStatus,
    ScribeOutputMessage, ScribeRequest, SerializableMessage,
    SerializableSession,
};
pub use crate::stream::ScribeStreamChunk;
use crate::stream::TokenStreamParser;
use crate::telemetry::{
    OperationOutcome, ScribeMetrics, ScribeMetricsSnapshot,
};
use crate::tools::calculator::{calculator, calculator_tool_with_callback};
use crate::tools::database::{
    query_database, query_database_tool_with_callback,
};

struct ModelRegistry<T> {
    default_alias: String,
    models: HashMap<String, T>,
}

impl<T> ModelRegistry<T> {
    fn new(
        default_alias: impl Into<String>,
        entries: impl IntoIterator<Item = (String, T)>,
    ) -> Result<Self> {
        let default_alias = default_alias.into();
        let mut models = HashMap::new();
        for (alias, model) in entries {
            if models.insert(alias.clone(), model).is_some() {
                return Err(anyhow!("duplicate loaded model alias: {alias}"));
            }
        }
        if !models.contains_key(default_alias.as_str()) {
            return Err(anyhow!(
                "default loaded model is unavailable: {default_alias}"
            ));
        }
        Ok(Self {
            default_alias,
            models,
        })
    }

    fn resolve(&self, alias: Option<&str>) -> Result<&T> {
        let alias = alias.unwrap_or(&self.default_alias);
        self.models
            .get(alias)
            .with_context(|| format!("unknown Scribe model alias: {alias}"))
    }
}

struct SessionState {
    conversation: Arc<RwLock<Conversation>>,
    generation: Mutex<()>,
    accepting_requests: AtomicBool,
}

struct ModelInput {
    history: MultimodalMessages,
    temperature: f64,
    grammar: Option<LlguidanceGrammar>,
}

#[derive(Clone)]
enum ModelStreamEvent {
    Token(String),
    Usage(u64),
    Error(String),
    Ignored,
}

type ModelEventStream<'a> =
    Pin<Box<dyn Stream<Item = ModelStreamEvent> + Send + 'a>>;

#[async_trait::async_trait]
trait ChatModel: Send + Sync {
    async fn stream<'a>(
        &'a self,
        input: ModelInput,
    ) -> Result<ModelEventStream<'a>>;
}

struct MistralChatModel(Model);

#[async_trait::async_trait]
#[cfg_attr(coverage, coverage(off))]
impl ChatModel for MistralChatModel {
    async fn stream<'a>(
        &'a self,
        input: ModelInput,
    ) -> Result<ModelEventStream<'a>> {
        let mut request = RequestBuilder::from(input.history)
            .set_sampler_temperature(input.temperature)
            .with_code_execution();
        if let Some(grammar) = input.grammar {
            request = request.set_constraint(Constraint::Llguidance(grammar));
        }
        let stream =
            self.0.stream_chat_request(request).await.map_err(|error| {
                anyhow!("failed to start model stream: {error:?}")
            })?;
        Ok(Box::pin(stream.flat_map(|response| {
            let events: [Option<ModelStreamEvent>; 2] = match response {
                Response::Chunk(ChatCompletionChunkResponse {
                    choices,
                    usage,
                    ..
                }) => {
                    let token = choices
                        .first()
                        .and_then(|choice| choice.delta.content.clone())
                        .map(ModelStreamEvent::Token);
                    let generated_tokens = usage.map(|usage| {
                        ModelStreamEvent::Usage(
                            u64::try_from(usage.completion_tokens)
                                .unwrap_or(u64::MAX),
                        )
                    });
                    [token, generated_tokens]
                },
                Response::InternalError(error) => {
                    [Some(ModelStreamEvent::Error(format!("{error:?}"))), None]
                },
                _ => [Some(ModelStreamEvent::Ignored), None],
            };
            futures::stream::iter(events.into_iter().flatten())
        })))
    }
}

impl SessionState {
    fn new(system_prompt: &str) -> Self {
        Self {
            conversation: Arc::new(RwLock::new(Conversation::new(
                system_prompt,
            ))),
            generation: Mutex::new(()),
            accepting_requests: AtomicBool::new(true),
        }
    }

    fn is_accepting_requests(&self) -> bool {
        self.accepting_requests.load(Ordering::Acquire)
    }

    fn close(&self) {
        self.accepting_requests.store(false, Ordering::Release);
    }
}

#[cfg_attr(coverage, coverage(off))]
pub(crate) async fn build_model(
    config: &ScribeConfig,
    model_config: &ScribeModelConfig,
    metrics: &Arc<ScribeMetrics>,
) -> Result<Model> {
    let paged_attn_config = PagedAttentionMetaBuilder::default()
        .with_block_size(32)
        .with_gpu_memory(mistralrs::MemoryGpuConfig::ContextSize(
            config.max_seq_len,
        ))
        .build()
        .map_err(|error| {
            anyhow!("failed to configure paged attention: {error:?}")
        })?;

    let (calculator_tool, _) = calculator_tool_with_callback();
    let (database_tool, _) = query_database_tool_with_callback();
    let calculator_metrics = Arc::clone(metrics);
    let calculator_callback = Arc::new(
        move |function: &mistralrs::CalledFunction,
              _context: &mistralrs::ToolCallContext| {
            let started_at = Instant::now();
            #[derive(Deserialize)]
            struct Args {
                expression: String,
            }
            let result = (|| {
                let args: Args = serde_json::from_str(&function.arguments)
                    .context("failed to parse calculator arguments")?;
                calculator(args.expression)
            })();
            let outcome = match &result {
                Ok(response) if !response.starts_with("Error:") => {
                    OperationOutcome::Success
                },
                Ok(_) | Err(_) => OperationOutcome::Error,
            };
            calculator_metrics.record_tool_call(
                started_at,
                "calculator",
                outcome,
            );
            result
        },
    );
    let database_metrics = Arc::clone(metrics);
    let database_callback = Arc::new(
        move |function: &mistralrs::CalledFunction,
              _context: &mistralrs::ToolCallContext| {
            let started_at = Instant::now();
            #[derive(Deserialize)]
            struct Args {
                question: String,
                keywords: Option<Vec<String>>,
                scope: Option<String>,
                max_results: Option<u16>,
            }
            let result = (|| {
                let args: Args = serde_json::from_str(&function.arguments)
                    .context("failed to parse database query arguments")?;
                let runtime = tokio::runtime::Handle::try_current().context(
                    "database tool requires an active Tokio runtime",
                )?;
                if !matches!(
                    runtime.runtime_flavor(),
                    tokio::runtime::RuntimeFlavor::MultiThread
                ) {
                    return Err(anyhow!(
                        "database tool requires a multi-thread Tokio runtime"
                    ));
                }
                tokio::task::block_in_place(|| {
                    runtime.block_on(query_database(
                        args.question,
                        args.keywords,
                        args.scope,
                        args.max_results,
                    ))
                })
            })();
            let outcome = if result.is_ok() {
                OperationOutcome::Success
            } else {
                OperationOutcome::Error
            };
            database_metrics.record_tool_call(
                started_at,
                "query_database",
                outcome,
            );
            result
        },
    );

    let mut builder = UqffMultimodalModelBuilder::new(
        &model_config.model_id,
        model_config.model_files.clone(),
    )
    .into_inner()
    .with_auto_isq(IsqBits::Four)
    .with_logging()
    .with_paged_attn(paged_attn_config)
    .with_tool_callback_and_tool(
        "calculator",
        calculator_callback,
        calculator_tool,
    )
    .with_tool_callback_and_tool(
        "query_database",
        database_callback,
        database_tool,
    );
    if let Some(assistant_model_id) = &model_config.assistant_model_id {
        builder =
            builder.with_mtp_model(assistant_model_id, model_config.n_predict);
    }

    builder.build().await.with_context(|| {
        format!("failed to load Scribe model '{}'", model_config.alias)
    })
}

/// Thread-safe orchestration engine for concurrent model and chat lifecycles.
pub struct ScribeEngine {
    models: ModelRegistry<Arc<dyn ChatModel>>,
    conversations: RwLock<HashMap<String, Arc<SessionState>>>,
    config: ScribeConfig,
    completion_tx: mpsc::Sender<ScribeCompletionEvent>,
    http_client: reqwest::Client,
    next_message_id: AtomicU64,
    metrics: Arc<ScribeMetrics>,
}

impl ScribeEngine {
    fn from_loaded_models(
        config: ScribeConfig,
        completion_tx: mpsc::Sender<ScribeCompletionEvent>,
        loaded_models: Vec<(String, Arc<dyn ChatModel>)>,
        metrics: Arc<ScribeMetrics>,
    ) -> Result<Arc<Self>> {
        let models = ModelRegistry::new(&config.default_model, loaded_models)?;
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(15))
            .build()
            .context("failed to build Scribe attachment client")?;
        let session_capacity = config.max_cached_sessions;
        Ok(Arc::new(Self {
            models,
            conversations: RwLock::new(HashMap::with_capacity(
                session_capacity,
            )),
            config,
            completion_tx,
            http_client,
            next_message_id: AtomicU64::new(1),
            metrics,
        }))
    }

    /// Loads every configured model and creates a bounded, non-evicting chat
    /// cache. Completed sessions must be explicitly unloaded after
    /// persistence.
    #[cfg_attr(coverage, coverage(off))]
    #[tracing::instrument(
        name = "scribe.engine.initialize",
        skip(config, completion_tx),
        fields(model_count = config.models.len())
    )]
    pub async fn new(
        config: ScribeConfig,
        completion_tx: mpsc::Sender<ScribeCompletionEvent>,
    ) -> Result<Arc<Self>> {
        config.validate()?;
        tracing::info!(
            event.name = "scribe.engine.initializing",
            model_count = config.models.len(),
            "initializing Scribe engine"
        );

        let metrics = ScribeMetrics::new(config.models.len())?;
        let mut loaded_models = Vec::with_capacity(config.models.len());
        for model_config in &config.models {
            let span = tracing::info_span!(
                "scribe.model.load",
                model.alias = %model_config.alias,
                model.id = %model_config.model_id,
            );
            let model = build_model(&config, model_config, &metrics)
                .instrument(span)
                .await?;
            loaded_models.push((
                model_config.alias.clone(),
                Arc::new(MistralChatModel(model)) as Arc<dyn ChatModel>,
            ));
        }
        tracing::info!(
            event.name = "scribe.engine.ready",
            model_count = config.models.len(),
            "Scribe engine ready"
        );
        Self::from_loaded_models(config, completion_tx, loaded_models, metrics)
    }

    async fn session_state(
        &self,
        session_id: &str,
    ) -> Result<Arc<SessionState>> {
        if session_id.trim().is_empty() {
            return Err(anyhow!("session ID must not be empty"));
        }
        if let Some(session) = self.conversations.read().await.get(session_id)
        {
            return Ok(Arc::clone(session));
        }

        let mut sessions = self.conversations.write().await;
        if let Some(session) = sessions.get(session_id) {
            return Ok(Arc::clone(session));
        }
        if sessions.len() >= self.config.max_cached_sessions {
            return Err(anyhow!(
                "Scribe session capacity reached; persist and unload an inactive session"
            ));
        }
        let session = Arc::new(SessionState::new(&self.config.system_prompt));
        sessions.insert(session_id.to_owned(), Arc::clone(&session));
        self.metrics.session_started();
        Ok(session)
    }

    /// Returns an allocation-free snapshot of the engine's runtime metrics.
    #[inline]
    pub fn metrics(&self) -> ScribeMetricsSnapshot {
        self.metrics.snapshot()
    }

    /// Returns the in-memory conversation handle for compatibility callers.
    #[tracing::instrument(
        name = "scribe.session.load",
        skip(self),
        fields(session.id = session_id)
    )]
    pub async fn load_conversation(
        &self,
        session_id: &str,
    ) -> Result<Arc<RwLock<Conversation>>> {
        Ok(Arc::clone(
            &self.session_state(session_id).await?.conversation,
        ))
    }

    /// Hydrates a persisted session without racing an in-flight generation.
    #[tracing::instrument(
        name = "scribe.session.hydrate",
        skip(self, session),
        fields(session.id = %session.session_id, session.revision = session.revision)
    )]
    pub async fn hydrate_conversation_from_db(
        &self,
        session: &SerializableSession,
    ) -> Result<()> {
        let restored =
            Conversation::from_serializable(session, &self.http_client)
                .await?;
        let state = self.session_state(&session.session_id).await?;
        let _generation = state.generation.lock().await;
        if !state.is_accepting_requests() {
            return Err(anyhow!(
                "session '{}' is being unloaded and cannot be hydrated",
                session.session_id
            ));
        }
        let mut current = state.conversation.write().await;
        if current.revision() > restored.revision() {
            return Err(anyhow!(
                "refusing stale session revision {} because revision {} is active",
                restored.revision(),
                current.revision()
            ));
        }
        if current.revision() == restored.revision() &&
            current.revision() != 0 &&
            current.serializable_history != restored.serializable_history
        {
            return Err(anyhow!(
                "session revision {} conflicts with active chat content",
                restored.revision()
            ));
        }
        *current = restored;
        tracing::info!(
            event.name = "scribe.session.hydrated",
            session.id = %session.session_id,
            session.revision = session.revision,
            "Scribe session hydrated"
        );
        Ok(())
    }

    /// Captures the latest fully committed revision. In-flight turns never
    /// appear partially in this snapshot.
    #[tracing::instrument(
        name = "scribe.session.snapshot",
        skip(self, session_id),
        fields(session.id = session_id.as_ref())
    )]
    pub async fn save_conversation(
        &self,
        session_id: impl AsRef<str>,
    ) -> Result<SerializableSession> {
        let session_id = session_id.as_ref();
        let state = self
            .conversations
            .read()
            .await
            .get(session_id)
            .cloned()
            .with_context(|| {
            format!("unknown Scribe session: {session_id}")
        })?;
        let snapshot = state.conversation.read().await.snapshot(session_id);
        Ok(snapshot)
    }

    /// Waits for any in-flight turn, removes the chat from memory, and returns
    /// the final persistence snapshot.
    #[tracing::instrument(
        name = "scribe.session.unload",
        skip(self, session_id),
        fields(session.id = session_id.as_ref())
    )]
    pub async fn unload_conversation(
        &self,
        session_id: impl AsRef<str>,
    ) -> Result<SerializableSession> {
        let session_id = session_id.as_ref();
        let state = self
            .conversations
            .read()
            .await
            .get(session_id)
            .cloned()
            .with_context(|| {
            format!("unknown Scribe session: {session_id}")
        })?;
        let _generation = state.generation.lock().await;
        state.close();
        let snapshot = state.conversation.read().await.snapshot(session_id);
        let removed = self.conversations.write().await.remove(session_id);
        if removed.is_none() {
            return Err(anyhow!(
                "Scribe session disappeared while it was being unloaded: {session_id}"
            ));
        }
        self.metrics.session_ended();
        Ok(snapshot)
    }

    /// Starts a request on the default model using an engine-generated ID.
    #[tracing::instrument(
        name = "scribe.chat.execute_default",
        skip(self, session_id, prompt, attachments, grammar_constraint),
        fields(session.id = session_id.as_ref())
    )]
    pub async fn execute_agent_step(
        self: &Arc<Self>,
        session_id: impl AsRef<str>,
        prompt: impl AsRef<str>,
        attachments: Vec<AttachmentUrl>,
        grammar_constraint: Option<LlguidanceGrammar>,
    ) -> Result<mpsc::Receiver<Result<ScribeStreamChunk>>> {
        let sequence = self.next_message_id.fetch_add(1, Ordering::Relaxed);
        self.execute_request(ScribeRequest {
            session_id: session_id.as_ref().to_owned(),
            message_id: format!("{}-{sequence}", session_id.as_ref()),
            model_alias: None,
            prompt: prompt.as_ref().to_owned(),
            attachments,
            grammar_constraint,
        })
        .await
    }

    /// Starts a reliable request with caller-provided model and message IDs.
    #[tracing::instrument(
        name = "scribe.chat.execute",
        skip(self, request),
        fields(
            session.id = %request.session_id,
            message.id = %request.message_id,
            model.alias = request.model_alias.as_deref().unwrap_or("default")
        )
    )]
    pub async fn execute_request(
        self: &Arc<Self>,
        request: ScribeRequest,
    ) -> Result<mpsc::Receiver<Result<ScribeStreamChunk>>> {
        if request.message_id.trim().is_empty() {
            return Err(anyhow!("message ID must not be empty"));
        }
        if request.prompt.trim().is_empty() {
            return Err(anyhow!("prompt must not be empty"));
        }
        let model_alias = request
            .model_alias
            .as_deref()
            .unwrap_or(&self.config.default_model)
            .to_owned();
        let model = Arc::clone(self.models.resolve(Some(&model_alias))?);
        let session = self.session_state(&request.session_id).await?;
        let (stream_tx, stream_rx) = mpsc::channel(256);
        let engine = Arc::clone(self);
        let span = tracing::info_span!(
            "scribe.chat.generate",
            gen_ai.operation.name = "chat",
            gen_ai.request.model = %model_alias,
            session.id = %request.session_id,
            message.id = %request.message_id,
        );
        std::mem::drop(tokio::spawn(
            async move {
                engine
                    .run_generation(
                        request,
                        model_alias,
                        model,
                        session,
                        stream_tx,
                    )
                    .await;
            }
            .instrument(span),
        ));
        Ok(stream_rx)
    }

    async fn run_generation(
        &self,
        mut request: ScribeRequest,
        model_alias: String,
        model: Arc<dyn ChatModel>,
        session: Arc<SessionState>,
        stream_tx: mpsc::Sender<Result<ScribeStreamChunk>>,
    ) {
        let started_at = Instant::now();
        let _generation = session.generation.lock().await;
        let _generation_activity = self.metrics.generation_started();
        if !session.is_accepting_requests() {
            self.fail_generation(
                &request,
                &model_alias,
                &session,
                stream_tx,
                started_at,
                anyhow!(
                    "session '{}' is being unloaded and cannot accept new requests",
                    request.session_id
                ),
            )
            .await;
            return;
        }

        let cached_turn = session
            .conversation
            .read()
            .await
            .cached_turn(&request.message_id);
        if let Some(cached) = cached_turn {
            if !cached.matches_request(
                &model_alias,
                &request.prompt,
                &request.attachments,
            ) {
                self.fail_generation(
                    &request,
                    &model_alias,
                    &session,
                    stream_tx,
                    started_at,
                    anyhow!(
                        "message ID '{}' was already used by a different request",
                        request.message_id
                    ),
                )
                .await;
                return;
            }
            if stream_tx
                .send(Ok(ScribeStreamChunk::Content(cached.response.clone())))
                .await
                .is_err()
            {
                tracing::debug!(
                    event.name = "scribe.stream.detached",
                    "generation client detached before idempotent replay"
                );
            }
            let snapshot = session
                .conversation
                .read()
                .await
                .snapshot(&request.session_id);
            drop(stream_tx);
            self.send_completion(ScribeCompletionEvent {
                message_id: request.message_id,
                model_alias: model_alias.clone(),
                status: ScribeCompletionStatus::Completed,
                session: snapshot,
                final_content: Some(cached.response),
                error: None,
                runtime_attachments: request.attachments,
            })
            .await;
            self.metrics.record_generation(
                started_at,
                &model_alias,
                OperationOutcome::Success,
                0,
            );
            return;
        }

        let resolved = match S3AttachmentResolver::resolve_all(
            &self.http_client,
            &request.attachments,
        )
        .await
        {
            Ok(resolved) => resolved,
            Err(error) => {
                self.fail_generation(
                    &request,
                    &model_alias,
                    &session,
                    stream_tx,
                    started_at,
                    error,
                )
                .await;
                return;
            },
        };
        let images = resolved
            .into_iter()
            .filter_map(|attachment| match attachment {
                Attachment::Image(image) => Some(image),
                Attachment::Audio(_) => None,
            })
            .collect::<Vec<_>>();
        let user_history = {
            let conversation = session.conversation.read().await;
            if images.is_empty() {
                conversation
                    .history
                    .clone()
                    .add_message(TextMessageRole::User, &request.prompt)
            } else {
                conversation.history.clone().add_image_message(
                    TextMessageRole::User,
                    &request.prompt,
                    images,
                )
            }
        };
        let mut model_stream = match model
            .stream(ModelInput {
                history: user_history.clone(),
                temperature: self.config.temperature,
                grammar: request.grammar_constraint.take(),
            })
            .await
        {
            Ok(stream) => stream,
            Err(error) => {
                self.fail_generation(
                    &request,
                    &model_alias,
                    &session,
                    stream_tx,
                    started_at,
                    error,
                )
                .await;
                return;
            },
        };
        let mut response = String::new();
        let mut generated_token_fragments = 0_u64;
        let mut reported_generated_tokens = None;
        let mut parser = TokenStreamParser::new();
        let mut parsed_chunks = Vec::with_capacity(2);
        let mut client_connected = true;

        while let Some(chunk) = model_stream.next().await {
            match chunk {
                ModelStreamEvent::Token(token) => {
                    if !token.is_empty() {
                        generated_token_fragments =
                            generated_token_fragments.saturating_add(1);
                    }
                    response.push_str(&token);
                    parsed_chunks.clear();
                    parser.advance(&token, &mut parsed_chunks);
                    if client_connected {
                        for parsed_chunk in parsed_chunks.drain(..) {
                            if stream_tx.send(Ok(parsed_chunk)).await.is_err()
                            {
                                client_connected = false;
                                tracing::debug!(
                                    event.name = "scribe.stream.detached",
                                    "generation client detached; generation will continue"
                                );
                                break;
                            }
                        }
                    }
                },
                ModelStreamEvent::Usage(generated_tokens) => {
                    reported_generated_tokens = Some(generated_tokens);
                },
                ModelStreamEvent::Error(error) => {
                    self.fail_generation(
                        &request,
                        &model_alias,
                        &session,
                        stream_tx,
                        started_at,
                        anyhow!("model stream failed: {error}"),
                    )
                    .await;
                    return;
                },
                ModelStreamEvent::Ignored => {},
            }
        }
        if client_connected &&
            let Some(final_chunk) = parser.flush() &&
            stream_tx.send(Ok(final_chunk)).await.is_err()
        {
            tracing::debug!(
                event.name = "scribe.stream.detached",
                "generation client detached during final chunk"
            );
        }

        let completed_history =
            user_history.add_message(TextMessageRole::Assistant, &response);
        let snapshot = {
            let mut conversation = session.conversation.write().await;
            let turn = CompletedTurn {
                message_id: &request.message_id,
                model_alias: &model_alias,
                prompt: &request.prompt,
                attachments: request.attachments.clone(),
                response: &response,
            };
            if let Err(error) = conversation.commit_turn(&turn) {
                drop(conversation);
                self.fail_generation(
                    &request,
                    &model_alias,
                    &session,
                    stream_tx,
                    started_at,
                    error,
                )
                .await;
                return;
            }
            conversation.history = completed_history;
            conversation.snapshot(&request.session_id)
        };
        drop(stream_tx);
        self.send_completion(ScribeCompletionEvent {
            message_id: request.message_id,
            model_alias: model_alias.clone(),
            status: ScribeCompletionStatus::Completed,
            session: snapshot,
            final_content: Some(response),
            error: None,
            runtime_attachments: request.attachments,
        })
        .await;
        self.metrics.record_generation(
            started_at,
            &model_alias,
            OperationOutcome::Success,
            reported_generated_tokens.unwrap_or(generated_token_fragments),
        );
        tracing::info!(
            event.name = "scribe.chat.completed",
            model.alias = %model_alias,
            "Scribe chat generation completed"
        );
    }

    async fn fail_generation(
        &self,
        request: &ScribeRequest,
        model_alias: &str,
        session: &SessionState,
        stream_tx: mpsc::Sender<Result<ScribeStreamChunk>>,
        started_at: Instant,
        error: anyhow::Error,
    ) {
        let error_message = error.to_string();
        if stream_tx
            .send(Err(anyhow!(error_message.clone())))
            .await
            .is_err()
        {
            tracing::debug!(
                event.name = "scribe.stream.detached",
                "generation client detached before failure notification"
            );
        }
        let snapshot = session
            .conversation
            .read()
            .await
            .snapshot(&request.session_id);
        drop(stream_tx);
        self.send_completion(ScribeCompletionEvent {
            message_id: request.message_id.clone(),
            model_alias: model_alias.to_owned(),
            status: ScribeCompletionStatus::Failed,
            session: snapshot,
            final_content: None,
            error: Some(error_message.clone()),
            runtime_attachments: request.attachments.clone(),
        })
        .await;
        self.metrics.record_generation(
            started_at,
            model_alias,
            OperationOutcome::Error,
            0,
        );
        tracing::error!(
            event.name = "scribe.chat.failed",
            error = %error_message,
            model.alias = %model_alias,
            "Scribe chat generation failed"
        );
    }

    async fn send_completion(&self, event: ScribeCompletionEvent) {
        if let Err(error) = self.completion_tx.send(event).await {
            self.metrics.record_completion_delivery_failure();
            tracing::error!(
                event.name = "scribe.completion.delivery.failed",
                message.id = %error.0.message_id,
                "completion receiver is unavailable"
            );
        }
    }
}

#[cfg(feature = "latex")]
pub use crate::report;

#[cfg(test)]
#[cfg_attr(coverage, coverage(off))]
#[path = "engine_tests.rs"]
mod tests;
