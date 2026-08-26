//! Agentic report generation and LaTeX rendering.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use anyhow::{Context as _, Result, anyhow};
use chrono::{Datelike, Local};
use mistralrs::{Agent, AgentBuilder, AgentStopReason};
use tera::{Tera, Value, to_value, try_get_value};

use crate::config::ScribeConfig;
use crate::engine::build_model;
use crate::telemetry::{
    OperationOutcome, ScribeMetrics, ScribeMetricsSnapshot,
};
use crate::tools::add_section::{
    SECTIONS, Section, add_section_tool_with_callback,
};
use crate::tools::calculator::calculator_tool_with_callback;
use crate::tools::database::{
    DatabaseProvider, query_database_tool_with_callback, set_database_provider,
};

/// Engine that orchestrates report generation and LaTeX rendering.
pub struct ScribeReport {
    agent: Agent,
    metrics: Arc<ScribeMetrics>,
}

impl ScribeReport {
    /// Create a new [`ScribeReport`] instance and initialize the mistralrs
    /// agent.
    #[tracing::instrument(
        name = "scribe.report.initialize",
        skip(config, db_provider)
    )]
    pub async fn new(
        config: ScribeConfig,
        db_provider: impl DatabaseProvider + 'static,
    ) -> Result<Self> {
        if let Err(err) = set_database_provider(db_provider) {
            tracing::warn!(?err, "failed to set database provider");
        }

        config.validate()?;
        let model_config = config
            .models
            .iter()
            .find(|model| model.alias == config.default_model)
            .context("default report model is not configured")?;
        let metrics = ScribeMetrics::new(1)?;
        let model = build_model(&config, model_config, &metrics).await?;

        let agent = AgentBuilder::new(model)
            .with_system_prompt(&config.system_prompt)
            .with_max_iterations(config.max_iterations)
            .with_parallel_tool_execution(true)
            .register_tool(add_section_tool_with_callback())
            .register_tool(query_database_tool_with_callback())
            .register_tool(calculator_tool_with_callback())
            .build();

        Ok(Self { agent, metrics })
    }

    /// Returns an allocation-free snapshot of report runtime metrics.
    #[inline]
    pub fn metrics(&self) -> ScribeMetricsSnapshot {
        self.metrics.snapshot()
    }

    /// Generate LaTeX sections from a user prompt using the Agentic loop.
    #[tracing::instrument(
        name = "scribe.report.generate_sections",
        skip(self, user_prompt)
    )]
    pub async fn generate_sections(
        &self,
        user_prompt: &str,
    ) -> Result<Vec<Section>> {
        let started_at = Instant::now();
        let _generation_activity = self.metrics.generation_started();
        let sections = Arc::new(Mutex::new(Vec::new()));
        let sections_clone = sections.clone();

        let response = match SECTIONS
            .scope(
                sections_clone,
                async move { self.agent.run(user_prompt).await },
            )
            .await
        {
            Ok(response) => response,
            Err(error) => {
                self.metrics.record_generation(
                    started_at,
                    "report",
                    OperationOutcome::Error,
                    0,
                );
                return Err(error);
            },
        };

        tracing::debug!(?response, "llm generation ended");

        if let AgentStopReason::Error(err) = response.stop_reason {
            self.metrics.record_generation(
                started_at,
                "report",
                OperationOutcome::Error,
                0,
            );
            anyhow::bail!("Agent encountered an error: {}", err);
        }

        let result = {
            let guard = sections
                .lock()
                .map_err(|err| anyhow!("Mutex poisoned: {}", err))?;
            guard.clone()
        };
        self.metrics.record_generation(
            started_at,
            "report",
            OperationOutcome::Success,
            0,
        );
        Ok(result)
    }

    /// Takes the generated sections and applies the Tera LaTeX template.
    #[tracing::instrument(
        name = "scribe.report.render_latex",
        skip(sections),
        fields(section_count = sections.len())
    )]
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
    pub(crate) fn latex_escape(
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
    #[tracing::instrument(
        name = "scribe.report.generate_pdf",
        skip(self, user_prompt)
    )]
    pub async fn generate_pdf(&self, user_prompt: &str) -> Result<Vec<u8>> {
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
