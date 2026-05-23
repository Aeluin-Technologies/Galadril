//! Custom engine of Scribe.
//! Use any model you want.

use anyhow::Result;
use mistralrs::{
    AutoDeviceMapParams, DeviceMapSetting, IsqBits, Model, ModelBuilder,
    TextMessageRole, TextMessages,
};

const DEFAULT_MODEL: &str = "Qwen/Qwen3-VL-4B-Instruct";
const DEFAULT_SYSTEM_PROMPT: &str = include_str!("../templates/system.txt");

fn resolve_model() -> String {
    std::env::var("BASE_MODEL").unwrap_or_else(|_| DEFAULT_MODEL.to_string())
}

fn resolve_system_prompt() -> Result<String> {
    match std::env::var("SYSTEM_PROMPT") {
        Ok(path) => {
            tracing::info!(?path, "loading system prompt from env path");
            std::fs::read_to_string(&path).map_err(Into::into)
        },
        Err(_) => Ok(DEFAULT_SYSTEM_PROMPT.to_string()),
    }
}

/// Configuration for the report generation engine.
/// Provides a builder pattern to configure the Agent and model.
#[derive(Clone)]
pub struct ScribeConfig {
    pub model_id: String,
    pub system_prompt: String,
    pub custom_template: Option<String>,
    pub max_iterations: usize,
    pub max_seq_len: usize,
}

impl ScribeConfig {
    /// Creates a default configuration.
    pub fn new() -> Result<Self> {
        Ok(Self {
            model_id: resolve_model(),
            system_prompt: resolve_system_prompt()?,
            custom_template: None,
            max_iterations: 20,
            max_seq_len: 4096,
        })
    }

    /// Set a custom number of max iterations for the Agentic loop.
    pub fn with_max_iterations(mut self, iterations: usize) -> Self {
        self.max_iterations = iterations;
        self
    }

    /// Set a custom maximum sequence length.
    pub fn with_max_seq_len(mut self, max_seq_len: usize) -> Self {
        self.max_seq_len = max_seq_len;
        self
    }
}

/// Model creation logic.
pub async fn build_model(config: &ScribeConfig) -> Result<Model> {
    let device_map = DeviceMapSetting::Auto(AutoDeviceMapParams::Multimodal {
        max_seq_len: config.max_seq_len,
        max_batch_size: AutoDeviceMapParams::DEFAULT_MAX_BATCH_SIZE,
        max_image_shape: (1400, 1400),
        max_num_images: 0,
    });

    tracing::info!(
        model_id = ?config.model_id,
        "initializing multimodal ModelBuilder with ISQ"
    );

    ModelBuilder::new(&config.model_id)
        .with_logging()
        .with_auto_isq(IsqBits::Four)
        .with_device_mapping(device_map)
        .build()
        .await
}

/// The chat engine for standard text-based interaction.
pub struct ScribeChat {
    model: Model,
    messages: TextMessages,
}

impl ScribeChat {
    /// Create a new [`ScribeChat`] instance and initialize messages.
    pub async fn new(config: &ScribeConfig) -> Result<Self> {
        let model = build_model(config).await?;

        let mut messages = TextMessages::new();
        if !config.system_prompt.is_empty() {
            messages = messages
                .add_message(TextMessageRole::System, &config.system_prompt);
        }

        Ok(Self { model, messages })
    }

    /// Send a user prompt to the model and update the conversation history.
    pub async fn chat(&mut self, prompt: &str) -> Result<String> {
        self.messages = self
            .messages
            .clone()
            .add_message(TextMessageRole::User, prompt);

        let response =
            self.model.send_chat_request(self.messages.clone()).await?;

        let reply = response.choices[0]
            .message
            .content
            .as_ref()
            .cloned()
            .unwrap_or_default();

        self.messages = self
            .messages
            .clone()
            .add_message(TextMessageRole::Assistant, &reply);

        Ok(reply)
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
    use crate::tools::calculator::calculator_tool_with_callback;
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
