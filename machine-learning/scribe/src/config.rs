//! Typed model presets and bounded Scribe runtime configuration.

use std::collections::HashSet;
use std::path::PathBuf;

use anyhow::{Result, anyhow};

const DEFAULT_SYSTEM_PROMPT: &str = include_str!("../templates/system.txt");

/// Configuration for one independently addressable local language model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScribeModelConfig {
    pub alias: String,
    pub model_id: String,
    pub model_files: Vec<PathBuf>,
    pub assistant_model_id: Option<String>,
    pub n_predict: Option<usize>,
}

/// Built-in model configurations with stable routing identities.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScribeModelPreset {
    /// Default Gemma writer model with its matching assistant tokenizer.
    Writer,
}

impl ScribeModelPreset {
    /// Returns the stable routing alias for this preset.
    pub const fn alias(self) -> &'static str {
        match self {
            Self::Writer => "writer",
        }
    }

    /// Expands this preset into the model loader configuration.
    pub fn model_config(self) -> ScribeModelConfig {
        match self {
            Self::Writer => ScribeModelConfig {
                alias: self.alias().to_owned(),
                model_id: "mistralrs-community/gemma-4-E2B-it-UQFF".to_owned(),
                model_files: vec![PathBuf::from("afq4-0.uqff")],
                assistant_model_id: Some(
                    "google/gemma-4-E2B-it-assistant".to_owned(),
                ),
                n_predict: Some(2),
            },
        }
    }
}

/// Configuration payload for bounded multi-model execution.
#[derive(Debug, Clone)]
pub struct ScribeConfig {
    pub models: Vec<ScribeModelConfig>,
    pub default_model: String,
    pub system_prompt: String,
    pub max_seq_len: usize,
    pub temperature: f64,
    pub max_cached_sessions: usize,
    pub max_iterations: usize,
}

impl ScribeConfig {
    /// Generates deterministic default library parameters.
    pub fn new() -> Result<Self> {
        let preset = ScribeModelPreset::Writer;
        Ok(Self {
            models: vec![preset.model_config()],
            default_model: preset.alias().to_owned(),
            system_prompt: DEFAULT_SYSTEM_PROMPT.to_string(),
            max_seq_len: 4096,
            temperature: 0.1,
            max_cached_sessions: 1000,
            max_iterations: 10,
        })
    }

    /// Rejects ambiguous routing and invalid bounded-resource settings.
    pub fn validate(&self) -> Result<()> {
        if self.models.is_empty() {
            return Err(anyhow!(
                "at least one Scribe model must be configured"
            ));
        }
        if self.max_cached_sessions == 0 {
            return Err(anyhow!(
                "max_cached_sessions must be greater than zero"
            ));
        }
        if self.max_seq_len == 0 {
            return Err(anyhow!("max_seq_len must be greater than zero"));
        }

        let mut aliases = HashSet::with_capacity(self.models.len());
        for model in &self.models {
            if model.alias.trim().is_empty() ||
                model.model_id.trim().is_empty()
            {
                return Err(anyhow!(
                    "model aliases and IDs must not be empty"
                ));
            }
            if model.model_files.is_empty() {
                return Err(anyhow!(
                    "model '{}' has no UQFF files configured",
                    model.alias
                ));
            }
            if !aliases.insert(model.alias.as_str()) {
                return Err(anyhow!("duplicate model alias: {}", model.alias));
            }
        }
        if !aliases.contains(self.default_model.as_str()) {
            return Err(anyhow!(
                "default model alias is not configured: {}",
                self.default_model
            ));
        }
        Ok(())
    }
}
