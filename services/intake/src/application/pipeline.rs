//! In-memory pipeline configuration parser.

use anyhow::{Context, Result};
use config::{Config, File, FileFormat};

use crate::domain::models::PipelineConfig;

/// Parses raw YAML payloads into structured pipeline models.
pub fn parse_tenant_pipeline(yaml_content: &str) -> Result<PipelineConfig> {
    let builder = Config::builder()
        .add_source(File::from_str(yaml_content, FileFormat::Yaml));

    let built_config = builder.build().context(
        "Failed to build dynamic config-rs layer for tenant pipeline",
    )?;

    let pipeline: PipelineConfig = built_config
        .try_deserialize()
        .context("Failed to deserialize PipelineConfig from YAML buffer")?;

    Ok(pipeline)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid_pipeline_string() {
        let yaml = r#"
            sources:
              - id: test_source
                topic: raw
                match_pattern: "^images/"
        "#;

        let config = parse_tenant_pipeline(yaml).unwrap();
        assert_eq!(config.sources.len(), 1);
        assert_eq!(config.sources[0].id, "test_source");
        assert_eq!(config.sources[0].parser, "metadata"); // default fallback.
    }

    #[test]
    fn fails_on_invalid_yaml() {
        let yaml = "sources: [ invalid {";
        assert!(parse_tenant_pipeline(yaml).is_err());
    }
}
