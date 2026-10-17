//! Versioned public metadata contains no execution authority or credentials.

use std::collections::HashSet;
use std::sync::OnceLock;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityKind {
    Input,
    Output,
    Model,
    Mcp,
    Skill,
    Algorithm,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityTarget {
    Pipeline,
    Scribe,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityOrigin {
    #[default]
    Plugin,
    User,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityExecution {
    #[default]
    Registered,
    Declared,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Compatibility {
    #[serde(default)]
    pub allowed_models: Vec<String>,
    #[serde(default)]
    pub requires: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Capability {
    pub id: String,
    pub plugin_id: Option<String>,
    pub plugin_version: Option<String>,
    #[serde(default)]
    pub origin: CapabilityOrigin,
    #[serde(default)]
    pub execution: CapabilityExecution,
    #[serde(default)]
    pub compatibility: Compatibility,
    pub kind: CapabilityKind,
    pub targets: Vec<CapabilityTarget>,
    pub name: String,
    pub description: String,
    pub parameters_schema: Value,
    pub step_type: Option<String>,
    pub model_class: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CapabilityCatalog {
    pub schema_version: u8,
    pub capabilities: Vec<Capability>,
}

fn valid_segment(value: &str) -> bool {
    value.as_bytes().first().is_some_and(u8::is_ascii_lowercase) &&
        value.bytes().all(|byte| {
            byte.is_ascii_lowercase() ||
                byte.is_ascii_digit() ||
                matches!(byte, b'_' | b'-')
        })
}

impl CapabilityCatalog {
    /// Rejects malformed deployment metadata before it enters public
    /// discovery.
    pub fn parse(bytes: &[u8]) -> Result<Self> {
        ensure!(bytes.len() <= 262144, "Capability catalog exceeds limit");
        let catalog: Self = serde_json::from_slice(bytes)
            .context("Invalid capability catalog")?;
        ensure!(
            catalog.schema_version == 1 && catalog.capabilities.len() <= 256,
            "Unsupported capability catalog"
        );
        let mut ids = HashSet::with_capacity(catalog.capabilities.len());
        for item in &catalog.capabilities {
            ensure!(ids.insert(item.id.as_str()), "Duplicate capability ID");
            ensure!(
                item.id.len() <= 128 &&
                    item.id.contains('.') &&
                    item.id.split('.').all(valid_segment),
                "Invalid capability ID"
            );
            match item.origin {
                CapabilityOrigin::Plugin => {
                    let plugin = item
                        .plugin_id
                        .as_deref()
                        .context("Missing plugin ID")?;
                    let version = item
                        .plugin_version
                        .as_deref()
                        .context("Missing plugin version")?;
                    ensure!(
                        plugin.len() <= 64 &&
                            valid_segment(plugin) &&
                            plugin != "user" &&
                            item.id.strip_prefix(plugin).is_some_and(
                                |suffix| suffix.starts_with('.')
                            ),
                        "Invalid plugin namespace"
                    );
                    ensure!(
                        !version.is_empty() && version.chars().count() <= 64,
                        "Invalid plugin version"
                    );
                },
                CapabilityOrigin::User => ensure!(
                    item.plugin_id.is_none() &&
                        item.plugin_version.is_none() &&
                        item.id.starts_with("user."),
                    "Invalid user provenance"
                ),
            }
            let constraints = &item.compatibility;
            ensure!(
                constraints.allowed_models.len() <= 32 &&
                    constraints.requires.len() <= 32,
                "Too many compatibility constraints"
            );
            let mut models =
                HashSet::with_capacity(constraints.allowed_models.len());
            for model in &constraints.allowed_models {
                ensure!(
                    !model.is_empty() &&
                        model.chars().count() <= 256 &&
                        models.insert(model.as_str()),
                    "Invalid allowed model"
                );
            }
            let mut requirements =
                HashSet::with_capacity(constraints.requires.len());
            for required in &constraints.requires {
                ensure!(
                    required.len() <= 128 &&
                        required.contains('.') &&
                        required.split('.').all(valid_segment) &&
                        required != &item.id &&
                        requirements.insert(required.as_str()),
                    "Invalid capability requirement"
                );
            }
            ensure!(
                !item.targets.is_empty() &&
                    item.targets.len() <= 2 &&
                    (item.targets.len() == 1 ||
                        item.targets.first() != item.targets.last()),
                "Invalid capability targets"
            );
            for (value, limit) in
                [(&item.name, 128), (&item.description, 4096)]
            {
                ensure!(
                    !value.is_empty() && value.chars().count() <= limit,
                    "Invalid capability metadata"
                );
            }
            ensure!(
                item.parameters_schema.get("type").and_then(Value::as_str) ==
                    Some("object"),
                "Expected object parameter schema"
            );
            #[derive(serde::Serialize)]
            struct Parameters<'a> {
                parameters_schema: &'a Value,
            }
            let parameters = Parameters {
                parameters_schema: &item.parameters_schema,
            };
            ensure!(
                serde_json::to_vec(&parameters)?.len() <= 16384,
                "Capability schema exceeds limit"
            );
            ensure!(
                (item.kind == CapabilityKind::Model) ==
                    item.model_class.is_some(),
                "Model class mismatch"
            );
            if let Some(model) = &item.model_class {
                ensure!(
                    !model.is_empty() && model.chars().count() <= 256,
                    "Invalid model class"
                );
            }
            if let Some(step) = &item.step_type {
                ensure!(
                    matches!(
                        step.as_str(),
                        "source" |
                            "inference" |
                            "resolve" |
                            "sink" |
                            "causal" |
                            "dbt"
                    ),
                    "Unsupported step type"
                );
            }
        }
        Ok(catalog)
    }
}

/// Shares the exact packaged Python manifest without importing
/// implementations.
pub fn builtin_catalog() -> Result<&'static CapabilityCatalog> {
    static CATALOG: OnceLock<Result<CapabilityCatalog, String>> =
        OnceLock::new();
    let catalog = CATALOG.get_or_init(|| {
        let manifest = include_bytes!(
            "../../../../platform/pipeline/src/galadril_pipeline/builtin_capabilities.json"
        );
        CapabilityCatalog::parse(manifest).map_err(|error| error.to_string())
    });
    catalog.as_ref().map_err(|error| anyhow::anyhow!("{error}"))
}

#[cfg(test)]
mod tests {
    use anyhow::{Result, ensure};
    use serde_json::json;

    use super::*;

    #[test]
    fn capabilities_builtin_contract_and_filters() -> Result<()> {
        let catalog = builtin_catalog()?;
        for kind in [
            CapabilityKind::Input,
            CapabilityKind::Output,
            CapabilityKind::Model,
            CapabilityKind::Algorithm,
        ] {
            ensure!(catalog.capabilities.iter().any(|item| item.kind == kind));
        }
        ensure!(
            catalog.capabilities.iter().all(|item| item
                .targets
                .contains(&CapabilityTarget::Pipeline))
        );
        Ok(())
    }

    #[test]
    fn capabilities_reject_duplicate_ids_unknown_fields_and_versions()
    -> Result<()> {
        let item = json!({"id":"law.algorithm.timeline","plugin_id":"law","plugin_version":"1.0.0","kind":"algorithm","targets":["pipeline","scribe"],"name":"Timeline","description":"Analyze chronology","parameters_schema":{"type":"object"}});
        let valid = json!({"schema_version":1,"capabilities":[item.clone()]});
        CapabilityCatalog::parse(&serde_json::to_vec(&valid)?)?;
        for invalid in [
            json!({"schema_version":1,"capabilities":[item.clone(),item.clone()]}),
            json!({"schema_version":2,"capabilities":[item.clone()]}),
            json!({"schema_version":1,"capabilities":[{"endpoint":"http://secret"}]}),
        ] {
            ensure!(
                CapabilityCatalog::parse(&serde_json::to_vec(&invalid)?)
                    .is_err()
            );
        }
        for update in [
            json!({"id":"other.timeline"}),
            json!({"targets":[]}),
            json!({"targets":["scribe","scribe"]}),
            json!({"parameters_schema":{"type":"string"}}),
            json!({"model_class":"anything"}),
        ] {
            let mut value = item.clone();
            if let (Some(target), Some(fields)) =
                (value.as_object_mut(), update.as_object())
            {
                target.extend(
                    fields
                        .iter()
                        .map(|(key, value)| (key.clone(), value.clone())),
                );
            }
            ensure!(
                CapabilityCatalog::parse(&serde_json::to_vec(
                    &json!({"schema_version":1,"capabilities":[value]})
                )?)
                .is_err()
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod contribution_tests {
    use anyhow::{Result, ensure};
    use serde_json::json;

    use super::*;

    #[test]
    fn capabilities_user_skills_and_model_constraints_roundtrip() -> Result<()>
    {
        let item = json!({"id":"user.skill.research","origin":"user","plugin_id":null,"plugin_version":null,"kind":"skill","execution":"declared","targets":["scribe"],"name":"Research","description":"Research workflow","parameters_schema":{"type":"object"},"compatibility":{"allowed_models":["legal"],"requires":["law.mcp.research"]}});
        let parse = |item: serde_json::Value| {
            CapabilityCatalog::parse(&serde_json::to_vec(
                &json!({"schema_version":1,"capabilities":[item]}),
            )?)
        };
        let catalog = parse(item.clone())?;
        let skill = catalog.capabilities.first().context("Missing skill")?;
        ensure!(skill.plugin_id.is_none());
        ensure!(skill.compatibility.allowed_models == ["legal"]);
        let mut bound = item.clone();
        if let Some(fields) = bound.as_object_mut() {
            fields.insert("execution".into(), json!("registered"));
        }
        parse(bound)?;
        for update in [
            json!({"origin":"plugin"}),
            json!({"plugin_id":"law"}),
            json!({"id":"law.skill.research"}),
            json!({"compatibility":{"allowed_models":["legal","legal"]}}),
            json!({"compatibility":{"requires":["user.skill.research"]}}),
        ] {
            let mut invalid = item.clone();
            if let (Some(target), Some(fields)) =
                (invalid.as_object_mut(), update.as_object())
            {
                target.extend(
                    fields
                        .iter()
                        .map(|(key, value)| (key.clone(), value.clone())),
                );
            }
            ensure!(parse(invalid).is_err());
        }
        Ok(())
    }
}
