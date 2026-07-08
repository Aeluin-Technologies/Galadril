//! Custom engine of Scribe.
//! Use any model you want.

use std::num::NonZeroUsize;
use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use futures::future::join_all;
use lru::LruCache;
use mistralrs::{
    ChatCompletionChunkResponse, Constraint, IsqBits, LlguidanceGrammar,
    Model, MultimodalMessages, PagedAttentionMetaBuilder, RequestBuilder,
    Response, TextMessageRole, UqffMultimodalModelBuilder,
};
use serde::{Deserialize, Serialize};
use tokio::sync::{RwLock, mpsc};

use crate::tools::calculator::{calculator, calculator_tool_with_callback};
use crate::tools::database::{
    from_database, from_database_tool_with_callback,
};

const MODEL_REPO: &str = "mistralrs-community/gemma-4-E2B-it-UQFF";
const MODEL_FILE: &str = "afq4-0.uqff";
const ASSISTANT_MODEL_REPO: &str = "google/gemma-4-E2B-it-assistant";
const DEFAULT_SYSTEM_PROMPT: &str = include_str!("../templates/system.txt");

/// Distinct streaming tokens categorized by inferential layer types.
#[derive(Debug, Clone)]
pub enum ScribeStreamChunk {
    Reasoning(String),
    Content(String),
}

/// Enumerates explicitly supported message participants.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MessageRole {
    System,
    User,
    Assistant,
}

impl From<MessageRole> for TextMessageRole {
    fn from(role: MessageRole) -> Self {
        match role {
            MessageRole::System => TextMessageRole::System,
            MessageRole::User => TextMessageRole::User,
            MessageRole::Assistant => TextMessageRole::Assistant,
        }
    }
}

/// Rich media attachments for GraphRAG and multi-turn interaction.
#[derive(Debug, Clone)]
pub enum Attachment {
    Image(image::DynamicImage),
    Audio(Vec<u8>),
}

/// Remote S3 resource pointers for media persistence.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "url")]
pub enum AttachmentUrl {
    Image(String),
    Audio(String),
}

/// Helper component to stream and transform S3 assets into memory frames.
pub struct S3AttachmentResolver;

impl S3AttachmentResolver {
    /// Downloads and decodes a single image from a remote URL.
    pub async fn resolve_image(
        client: &reqwest::Client,
        url: &str,
    ) -> Result<image::DynamicImage> {
        let response = client.get(url).send().await.with_context(|| {
            format!("Failed to dispatch request to S3 target: {}", url)
        })?;

        if !response.status().is_success() {
            return Err(anyhow!(
                "S3 returned status code error: {}",
                response.status()
            ));
        }

        let raw_bytes = response.bytes().await.with_context(
            || "Error reading full bytes from S3 network stream",
        )?;

        image::load_from_memory(&raw_bytes)
            .with_context(|| format!("Failed decoding image structural bytes into a matrix from: {}", url))
    }

    /// Concurrently resolves a slice of attachment URLs into memory assets.
    pub async fn resolve_all(
        client: &reqwest::Client,
        targets: &[AttachmentUrl],
    ) -> Vec<Attachment> {
        let futures = targets.iter().map(|target| async move {
            match target {
                AttachmentUrl::Image(url) => {
                    match Self::resolve_image(client, url).await {
                        Ok(img) => Some(Attachment::Image(img)),
                        Err(err) => {
                            tracing::error!(error = ?err, url = %url, "S3 attachment resolution collapsed");
                            None
                        }
                    }
                }
                AttachmentUrl::Audio(url) => {
                    match client.get(url).send().await {
                        Ok(resp) => match resp.bytes().await {
                            Ok(audio_bytes) if !audio_bytes.is_empty() => {
                                Some(Attachment::Audio(audio_bytes.to_vec()))
                            }
                            _ => None,
                        },
                        Err(_) => None,
                    }
                }
            }
        });

        join_all(futures).await.into_iter().flatten().collect()
    }
}

/// A serializable entity representation optimized for zero-trust document and
/// SQL/NoSQL databases.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SerializableMessage {
    pub role: MessageRole,
    pub content: String,
    pub attachments: Vec<AttachmentUrl>,
}

/// Database-ready serializable wrapper matching the target schema.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SerializableSession {
    pub session_id: String,
    pub messages: Vec<SerializableMessage>,
}

/// Production message envelope emitted asynchronously when generation
/// lifecycle terminates.
#[derive(Debug, Clone)]
pub struct ScribeOutputMessage {
    pub session_id: String,
    pub final_content: String,
    pub runtime_attachments: Vec<AttachmentUrl>,
}

/// Represents a stateful, thread-safe sequence of multi-modal messages.
#[derive(Clone)]
pub struct Conversation {
    pub history: MultimodalMessages,
    pub serializable_history: Vec<SerializableMessage>,
}

impl Conversation {
    /// Creates a new conversation initialized with an optional system prompt.
    pub fn new(system_prompt: &str) -> Self {
        let mut history = MultimodalMessages::new();
        let mut serializable_history = Vec::new();

        if !system_prompt.is_empty() {
            history =
                history.add_message(TextMessageRole::System, system_prompt);
            serializable_history.push(SerializableMessage {
                role: MessageRole::System,
                content: system_prompt.to_string(),
                attachments: Vec::new(),
            });
        }

        Self {
            history,
            serializable_history,
        }
    }

    /// Hydrates a full multi-modal conversation from a serialized database
    /// schema session.
    pub async fn from_serializable(
        session: &SerializableSession,
        http_client: &reqwest::Client,
    ) -> Result<Self> {
        let mut history = MultimodalMessages::new();

        for msg in &session.messages {
            let role = TextMessageRole::from(msg.role);

            if !msg.attachments.is_empty() {
                let resolved = S3AttachmentResolver::resolve_all(
                    http_client,
                    &msg.attachments,
                )
                .await;

                let images: Vec<_> = resolved
                    .into_iter()
                    .filter_map(|att| match att {
                        Attachment::Image(img) => Some(img),
                        _ => None,
                    })
                    .collect();

                if !images.is_empty() {
                    history =
                        history.add_image_message(role, &msg.content, images);
                    continue;
                }
            }

            history = history.add_message(role, &msg.content);
        }

        Ok(Self {
            history,
            serializable_history: session.messages.clone(),
        })
    }
}

/// Configuration payload engineered for low-latency and deterministic
/// server-side executions.
#[derive(Clone)]
pub struct ScribeConfig {
    pub model_id: String,
    pub assistant_model_id: String,
    pub system_prompt: String,
    pub max_seq_len: usize,
    pub temperature: f64,
    pub n_predict: usize,
    pub max_cached_sessions: usize,
    pub max_iterations: usize,
}

impl ScribeConfig {
    /// Generates default server engine parameters overridden by environment
    /// system values.
    pub fn new() -> Result<Self> {
        let system_prompt = match std::env::var("SYSTEM_PROMPT") {
            Ok(path) => {
                tracing::info!(env_path = ?path, "Loading system prompt from environment path");
                std::fs::read_to_string(&path).with_context(|| {
                    format!("Failed to read system prompt file at: {}", path)
                })?
            },
            Err(_) => DEFAULT_SYSTEM_PROMPT.to_string(),
        };

        Ok(Self {
            model_id: MODEL_REPO.to_string(),
            assistant_model_id: ASSISTANT_MODEL_REPO.to_string(),
            system_prompt,
            max_seq_len: 4096,
            temperature: 0.1,
            n_predict: 2,
            max_cached_sessions: 1000,
            max_iterations: 10,
        })
    }
}

/// Clean flat state machine processing network stream boundaries cleanly
/// without deep brackets.
struct TokenStreamParser {
    buffer: String,
    in_reasoning: bool,
}

impl TokenStreamParser {
    const END_TAG: &'static str = "</reasoning>";
    const START_TAG: &'static str = "<reasoning>";

    fn new() -> Self {
        Self {
            buffer: String::new(),
            in_reasoning: false,
        }
    }

    fn get_partial_match_len(&self, tag: &str) -> usize {
        for len in (1..tag.len()).rev() {
            if self.buffer.ends_with(&tag[..len]) {
                return len;
            }
        }
        0
    }

    fn advance(&mut self, token: &str, output: &mut Vec<ScribeStreamChunk>) {
        self.buffer.push_str(token);

        loop {
            if !self.in_reasoning {
                if let Some(idx) = self.buffer.find(Self::START_TAG) {
                    let content = &self.buffer[..idx];
                    if !content.is_empty() {
                        output.push(ScribeStreamChunk::Content(
                            content.to_string(),
                        ));
                    }
                    self.in_reasoning = true;
                    self.buffer =
                        self.buffer[idx + Self::START_TAG.len()..].to_string();
                    continue;
                }

                let partial = self.get_partial_match_len(Self::START_TAG);
                let flush_len = self.buffer.len() - partial;
                if flush_len > 0 {
                    output.push(ScribeStreamChunk::Content(
                        self.buffer[..flush_len].to_string(),
                    ));
                    self.buffer = self.buffer[flush_len..].to_string();
                }
            } else {
                if let Some(idx) = self.buffer.find(Self::END_TAG) {
                    let reasoning = &self.buffer[..idx];
                    if !reasoning.is_empty() {
                        output.push(ScribeStreamChunk::Reasoning(
                            reasoning.to_string(),
                        ));
                    }
                    self.in_reasoning = false;
                    self.buffer =
                        self.buffer[idx + Self::END_TAG.len()..].to_string();
                    continue;
                }

                let partial = self.get_partial_match_len(Self::END_TAG);
                let flush_len = self.buffer.len() - partial;
                if flush_len > 0 {
                    output.push(ScribeStreamChunk::Reasoning(
                        self.buffer[..flush_len].to_string(),
                    ));
                    self.buffer = self.buffer[flush_len..].to_string();
                }
            }
            break;
        }
    }

    fn flush(self) -> Option<ScribeStreamChunk> {
        if self.buffer.is_empty() {
            return None;
        }
        match self.in_reasoning {
            true => Some(ScribeStreamChunk::Reasoning(self.buffer)),
            false => Some(ScribeStreamChunk::Content(self.buffer)),
        }
    }
}

/// Thread-safe orchestration engine managing model lifecycle, sandboxing, and
/// context management.
pub struct ScribeEngine {
    model: Arc<Model>,
    conversations: RwLock<LruCache<String, Arc<RwLock<Conversation>>>>,
    config: ScribeConfig,
    persistence_tx: mpsc::Sender<ScribeOutputMessage>,
    http_client: reqwest::Client,
}

impl ScribeEngine {
    /// Instantiates the inference engine, allocates the GPU PagedAttention
    /// pools, registers native tools, and sets up cache bounds.
    pub async fn new(
        config: ScribeConfig,
        persistence_tx: mpsc::Sender<ScribeOutputMessage>,
    ) -> Result<Arc<Self>> {
        tracing::info!("Initializing Scribe Engine runtime infrastructure");

        let paged_attn_config = PagedAttentionMetaBuilder::default()
            .with_block_size(32)
            .with_gpu_memory(mistralrs::MemoryGpuConfig::ContextSize(
                config.max_seq_len,
            ))
            .build()
            .map_err(|e| {
                anyhow!(
                    "Failed to initialize PagedAttention memory maps: {:?}",
                    e
                )
            })?;

        // Extract structural schemas safely generated by macro blueprints
        let (calc_tool, _) = calculator_tool_with_callback();
        let (db_tool, _) = from_database_tool_with_callback();

        // Build manual synchronous tool callback wrappers to perfectly match
        // the expected `Arc<ToolCallback>` signature of
        // UqffMultimodalModelBuilder.
        let calc_callback = Arc::new(
            |f: &mistralrs::CalledFunction,
             _ctx: &mistralrs::ToolCallContext| {
                #[derive(serde::Deserialize)]
                struct Args {
                    expression: String,
                }
                let args: Args =
                    serde_json::from_str(&f.arguments).map_err(|e| {
                        anyhow!("Failed to parse calculator arguments: {}", e)
                    })?;
                calculator(args.expression)
            },
        );

        let db_callback = Arc::new(
            |f: &mistralrs::CalledFunction,
             _ctx: &mistralrs::ToolCallContext| {
                #[derive(serde::Deserialize)]
                struct Args {
                    query: String,
                }
                let args: Args =
                    serde_json::from_str(&f.arguments).map_err(|e| {
                        anyhow!("Failed to parse database arguments: {}", e)
                    })?;

                // Safe transition out of active worker thread limits into a
                // blocking section
                tokio::task::block_in_place(|| {
                    tokio::runtime::Handle::current()
                        .block_on(async { from_database(args.query).await })
                })
            },
        );

        let model_files = vec![std::path::PathBuf::from(MODEL_FILE)];
        let model = UqffMultimodalModelBuilder::new(
            config.model_id.clone(),
            model_files,
        )
        .into_inner()
        .with_auto_isq(IsqBits::Four)
        .with_logging()
        .with_paged_attn(paged_attn_config)
        // Chain the tools into model engine capabilities
        .with_tool_callback_and_tool("calculator", calc_callback, calc_tool)
        .with_tool_callback_and_tool("from_database", db_callback, db_tool)
        .build()
        .await
        .map_err(|e| {
            anyhow!("UQFF Model builder initialization collapsed: {:?}", e)
        })?;

        let cache_capacity = NonZeroUsize::new(config.max_cached_sessions)
            .unwrap_or_else(|| NonZeroUsize::new(100).unwrap());

        let http_client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .build()?;

        Ok(Arc::new(Self {
            model: Arc::new(model),
            conversations: RwLock::new(LruCache::new(cache_capacity)),
            config,
            persistence_tx,
            http_client,
        }))
    }

    /// Fetches an existing active conversation session or registers a new
    /// context tracked inside the LRU cache.
    pub async fn load_conversation(
        &self,
        session_id: &str,
    ) -> Arc<RwLock<Conversation>> {
        let mut lock = self.conversations.write().await;
        if let Some(conv) = lock.get(session_id) {
            conv.clone()
        } else {
            let conv = Arc::new(RwLock::new(Conversation::new(
                &self.config.system_prompt,
            )));
            lock.put(session_id.to_string(), conv.clone());
            conv
        }
    }

    /// Injects an externally stored historical session payload into active
    /// operational cache memory.
    pub async fn hydrate_conversation_from_db(
        &self,
        session: &SerializableSession,
    ) -> Result<()> {
        let conversation =
            Conversation::from_serializable(session, &self.http_client)
                .await?;
        let mut lock = self.conversations.write().await;
        lock.put(
            session.session_id.clone(),
            Arc::new(RwLock::new(conversation)),
        );
        Ok(())
    }

    /// Pulls current tracking state out of the engine's active memory cache
    /// for serialization.
    pub async fn save_conversation(
        &self,
        session_id: impl AsRef<str>,
    ) -> Result<SerializableSession> {
        let mut lock = self.conversations.write().await;
        if let Some(conv_arc) = lock.get(session_id.as_ref()) {
            let conv = conv_arc.read().await;
            Ok(SerializableSession {
                session_id: session_id.as_ref().to_string(),
                messages: conv.serializable_history.clone(),
            })
        } else {
            Err(anyhow!(
                "Session ID context completely missing or evicted from LRU storage cache"
            ))
        }
    }

    /// Submits a multi-modal user query request to the model pipeline and
    /// yields a channel receiver streaming output chunks.
    pub async fn execute_agent_step(
        &self,
        session_id: impl AsRef<str>,
        prompt: impl AsRef<str>,
        s3_attachments: Vec<AttachmentUrl>,
        grammar_constraint: Option<LlguidanceGrammar>,
    ) -> Result<mpsc::Receiver<Result<ScribeStreamChunk>>> {
        let session_key = session_id.as_ref().to_string();
        let conversation_arc = self.load_conversation(&session_key).await;

        let resolved_attachments = S3AttachmentResolver::resolve_all(
            &self.http_client,
            &s3_attachments,
        )
        .await;

        let images: Vec<_> = resolved_attachments
            .into_iter()
            .filter_map(|attachment| match attachment {
                Attachment::Image(img) => Some(img),
                _ => None,
            })
            .collect();

        let history_snapshot = {
            let mut conv = conversation_arc.write().await;

            conv.history = match !images.is_empty() {
                true => conv.history.clone().add_image_message(
                    TextMessageRole::User,
                    prompt.as_ref(),
                    images,
                ),
                false => conv
                    .history
                    .clone()
                    .add_message(TextMessageRole::User, prompt.as_ref()),
            };

            conv.serializable_history.push(SerializableMessage {
                role: MessageRole::User,
                content: prompt.as_ref().to_string(),
                attachments: s3_attachments.clone(),
            });

            conv.history.clone()
        };

        let mut request = RequestBuilder::from(history_snapshot)
            .set_sampler_temperature(self.config.temperature)
            .with_code_execution();

        if let Some(grammar) = grammar_constraint {
            request = request.set_constraint(Constraint::Llguidance(grammar));
        }

        let (tx, rx) = mpsc::channel(256);
        let persistence_sink = self.persistence_tx.clone();
        let model = self.model.clone();

        tokio::spawn(async move {
            let mut internal_stream = match model
                .stream_chat_request(request)
                .await
            {
                Ok(s) => s,
                Err(e) => {
                    let _ = tx.send(Err(anyhow!("Failed to establish low-level stream pipeline: {:?}", e))).await;
                    return;
                },
            };

            let mut full_response_accumulator = String::new();
            let mut parser = TokenStreamParser::new();

            while let Some(chunk) = internal_stream.next().await {
                match chunk {
                    Response::Chunk(ChatCompletionChunkResponse {
                        choices,
                        ..
                    }) => {
                        if let Some(choice) = choices.first() &&
                            let Some(ref text_token) = choice.delta.content
                        {
                            full_response_accumulator.push_str(text_token);

                            let mut chunks = Vec::new();
                            parser.advance(text_token, &mut chunks);

                            for chunk_item in chunks {
                                if tx.send(Ok(chunk_item)).await.is_err() {
                                    return;
                                }
                            }
                        }
                    },
                    Response::InternalError(err) => {
                        let _ = tx
                            .send(Err(anyhow!(
                                "Pipeline execution internal panic: {:?}",
                                err
                            )))
                            .await;
                        return;
                    },
                    _ => {},
                }
            }

            if let Some(final_chunk) = parser.flush() &&
                tx.send(Ok(final_chunk)).await.is_err()
            {
                return;
            }

            {
                let mut conv = conversation_arc.write().await;
                conv.history = conv.history.clone().add_message(
                    TextMessageRole::Assistant,
                    &full_response_accumulator,
                );

                conv.serializable_history.push(SerializableMessage {
                    role: MessageRole::Assistant,
                    content: full_response_accumulator.clone(),
                    attachments: Vec::new(),
                });
            }

            let out_message = ScribeOutputMessage {
                session_id: session_key,
                final_content: full_response_accumulator,
                runtime_attachments: s3_attachments,
            };

            let _ = persistence_sink.send(out_message).await;
        });

        Ok(rx)
    }
}

#[cfg(feature = "latex")]
pub mod report {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use anyhow::anyhow;
    use chrono::{Datelike, Local};
    use mistralrs::{Agent, AgentBuilder, AgentStopReason};
    use tera::{Tera, Value, to_value, try_get_value};

    use super::*;
    use crate::tools::add_section::{
        SECTIONS, Section, add_section_tool_with_callback,
    };
    use crate::tools::database::{
        DatabaseProvider, from_database_tool_with_callback,
        set_database_provider,
    };

    /// Engine that orchestrates report generation and LaTeX rendering.
    pub struct ScribeReport {
        config: ScribeConfig,
        agent: Agent,
    }

    impl ScribeReport {
        /// Create a new [`ScribeReport`] instance and initialize the mistralrs
        /// agent.
        pub async fn new(
            config: ScribeConfig,
            db_provider: impl DatabaseProvider + 'static,
        ) -> Result<Self> {
            if let Err(err) = set_database_provider(db_provider) {
                tracing::warn!(?err, "failed to set database provider");
            }

            // Use the shared function to instantiate the MistralRS model
            let model = build_model(&config).await?;

            let agent = AgentBuilder::new(model)
                .with_system_prompt(&config.system_prompt)
                .with_max_iterations(config.max_iterations)
                .with_parallel_tool_execution(true)
                .register_tool(add_section_tool_with_callback())
                .register_tool(from_database_tool_with_callback())
                .register_tool(calculator_tool_with_callback())
                .build();

            Ok(Self { config, agent })
        }

        /// Generate LaTeX sections from a user prompt using the Agentic loop.
        pub async fn generate_sections(
            &self,
            user_prompt: &str,
        ) -> Result<Vec<Section>> {
            let sections = Arc::new(Mutex::new(Vec::new()));
            let sections_clone = sections.clone();

            let response = SECTIONS
                .scope(sections_clone, async move {
                    self.agent.run(user_prompt).await
                })
                .await?;

            tracing::debug!(?response, "llm generation ended");

            if let AgentStopReason::Error(err) = response.stop_reason {
                anyhow::bail!("Agent encountered an error: {}", err);
            }

            let result = {
                let guard = sections
                    .lock()
                    .map_err(|err| anyhow!("Mutex poisoned: {}", err))?;
                guard.clone()
            };
            Ok(result)
        }

        /// Takes the generated sections and applies the Tera LaTeX template.
        pub fn generate_raw_latex(sections: Vec<Section>) -> Result<String> {
            let mut tera = Tera::default();
            let report_template = include_str!("../templates/report.tex");
            tera.add_raw_template("report.tex", report_template)?;
            tera.register_filter("latex_escape", Self::latex_escape);

            let mut context = tera::Context::new();
            context.insert("sections", &sections);

            let now = Local::now();
            context.insert("year", &now.year());
            context.insert("month", &now.month());
            context.insert("day", &now.day());

            let raw = tera.render("report.tex", &context)?;
            Ok(raw)
        }

        /// Tera filter to escape LaTeX special characters from user/model
        /// input.
        fn latex_escape(
            value: &Value,
            _: &HashMap<String, Value>,
        ) -> tera::Result<Value> {
            let s = try_get_value!("latex_escape", "value", String, value);
            let escaped = s.replace('&', "\\&").replace('%', "\\%");
            match to_value(escaped) {
                Ok(val) => Ok(val),
                Err(err) => Err(tera::Error::msg(format!(
                    "Failed to parse escaped value: {}",
                    err
                ))),
            }
        }

        /// Generate bytes of PDF of LaTeX article using a prompt via tectonic.
        pub async fn generate_pdf(
            &self,
            user_prompt: &str,
        ) -> Result<Vec<u8>> {
            let sections = self.generate_sections(user_prompt).await?;
            let raw_latex = Self::generate_raw_latex(sections)?;

            let pdf_bytes = tokio::task::spawn_blocking(move || {
                tectonic::latex_to_pdf(raw_latex).map_err(|err| {
                    anyhow!("Tectonic PDF compilation error: {err:?}")
                })
            })
            .await??;

            Ok(pdf_bytes)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn test_conversation_new() {
        let conv = Conversation::new("System prompt");
        assert_eq!(conv.serializable_history.len(), 1);
        assert_eq!(conv.serializable_history[0].role, MessageRole::System);

        let conv_empty = Conversation::new("");
        assert_eq!(conv_empty.serializable_history.len(), 0);
    }

    #[test]
    fn test_scribe_config_defaults() {
        let config = ScribeConfig::new().unwrap();
        assert_eq!(config.max_seq_len, 4096);
        assert_eq!(config.temperature, 0.1);
        assert_eq!(config.n_predict, 2);
        assert_eq!(config.max_cached_sessions, 1000);
        assert_eq!(config.max_iterations, 10);
    }

    #[test]
    fn test_parser_pure_content() {
        let mut parser = TokenStreamParser::new();
        let mut output = Vec::new();
        parser.advance("Hello ", &mut output);
        parser.advance("world!", &mut output);

        assert_eq!(output.len(), 2);
        match &output[0] {
            ScribeStreamChunk::Content(s) => assert_eq!(s, "Hello "),
            _ => panic!("Expected Content chunk"),
        }
        match &output[1] {
            ScribeStreamChunk::Content(s) => assert_eq!(s, "world!"),
            _ => panic!("Expected Content chunk"),
        }
        assert!(parser.flush().is_none());
    }

    #[test]
    fn test_parser_flush_partial_match() {
        let mut parser = TokenStreamParser::new();
        let mut output = Vec::new();
        parser.advance("Hello <reas", &mut output);

        assert_eq!(output.len(), 1);
        match &output[0] {
            ScribeStreamChunk::Content(s) => assert_eq!(s, "Hello "),
            _ => panic!("Expected Content chunk before partial match"),
        }

        if let Some(ScribeStreamChunk::Content(res)) = parser.flush() {
            assert_eq!(res, "<reas");
        } else {
            panic!("Expected flushed partial content remaining in buffer");
        }
    }

    #[test]
    fn test_parser_with_reasoning() {
        let mut parser = TokenStreamParser::new();
        let mut output = Vec::new();
        parser.advance("<reasoning>Thinking</reasoning>Done", &mut output);

        assert_eq!(output.len(), 2);
        match &output[0] {
            ScribeStreamChunk::Reasoning(r) => assert_eq!(r, "Thinking"),
            _ => panic!("Expected reasoning chunk"),
        }
        match &output[1] {
            ScribeStreamChunk::Content(c) => assert_eq!(c, "Done"),
            _ => panic!("Expected content chunk"),
        }
    }

    #[cfg(feature = "latex")]
    #[test]
    fn test_latex_escape() {
        use tera::to_value;
        let val = to_value("Context & Percent %").unwrap();
        let escaped = report::ScribeReport::latex_escape(
            &val,
            &std::collections::HashMap::new(),
        )
        .unwrap();
        assert_eq!(escaped.as_str().unwrap(), "Context \\& Percent \\%");
    }
}
