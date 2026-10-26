//! Validated, bounded artifacts independent of storage, IAM, and execution.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const MAX_PROJECT_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_FRAME: usize = MAX_PROJECT_BYTES * 5;
pub const MAX_PDF_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Personal,
    Project,
    Tenant,
    Plugin,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Asset {
    pub path: String,
    pub data: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VariableKind {
    Text,
    Integer,
    Boolean,
    Choice,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Variable {
    pub kind: VariableKind,
    pub required: bool,
    pub choices: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Template {
    pub id: String,
    pub version: String,
    pub name: String,
    pub description: String,
    pub scope: Scope,
    pub owner: String,
    pub project: Option<String>,
    pub plugin: Option<String>,
    pub entrypoint: String,
    pub assets: Vec<Asset>,
    pub variables: BTreeMap<String, Variable>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Document {
    #[serde(default)]
    pub variables: BTreeMap<String, serde_json::Value>,
    pub source: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rendered {
    pub entrypoint: Vec<u8>,
    pub assets: Vec<Asset>,
}

/// Restricts object identifiers before they become storage key components.
pub fn validate_id(value: &str, length: usize) -> Result<()> {
    ensure!(
        value.len() == length &&
            value
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
        "Invalid immutable identifier"
    );
    Ok(())
}

/// Uses the same canonical tenant alphabet as Gateway without normalizing
/// aliases.
pub fn validate_tenant(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty() &&
            value.len() <= 64 &&
            value.bytes().all(
                |c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-')
            ),
        "Invalid tenant identifier"
    );
    Ok(())
}

/// A virtual project never accepts URLs, platform paths, or executable assets.
pub fn validate_path(path: &str) -> Result<()> {
    ensure!(
        !path.is_empty() &&
            path.len() <= 192 &&
            path.split('/').all(|part| !part.is_empty() &&
                !matches!(part, "." | "..") &&
                !part.starts_with('.')),
        "Invalid asset path"
    );
    ensure!(
        path.bytes().all(|c| c.is_ascii_alphanumeric() ||
            matches!(c, b'_' | b'-' | b'.' | b'/')),
        "Invalid asset alphabet"
    );
    let extension = path
        .rsplit_once('.')
        .map(|(_, ext)| ext)
        .context("Asset requires an extension")?;
    ensure!(
        matches!(
            extension,
            "tex" |
                "cls" |
                "sty" |
                "bib" |
                "bst" |
                "png" |
                "jpg" |
                "jpeg" |
                "pdf" |
                "ttf" |
                "otf"
        ),
        "Unsupported asset type"
    );
    Ok(())
}

/// Escapes each scalar once so inserted values cannot be interpreted as
/// macros.
pub fn escape_text(value: &str) -> String {
    let mut result = String::with_capacity(value.len());
    for c in value.chars() {
        match c {
            '\\' => result.push_str("\\textbackslash{}"),
            '{' => result.push_str("\\{"),
            '}' => result.push_str("\\}"),
            '%' => result.push_str("\\%"),
            '&' => result.push_str("\\&"),
            '#' => result.push_str("\\#"),
            '_' => result.push_str("\\_"),
            '$' => result.push_str("\\$"),
            '^' => result.push_str("\\textasciicircum{}"),
            '~' => result.push_str("\\textasciitilde{}"),
            c if c.is_control() && c != '\n' && c != '\t' => {},
            c => result.push(c),
        }
    }
    result
}

impl Variable {
    fn render(&self, value: &serde_json::Value) -> Result<String> {
        match self.kind {
            VariableKind::Text => {
                let text = value.as_str().context("Text variable required")?;
                ensure!(text.len() <= 32768, "Text variable exceeds limit");
                Ok(escape_text(text))
            },
            VariableKind::Integer => Ok(value
                .as_i64()
                .context("Integer variable required")?
                .to_string()),
            VariableKind::Boolean => {
                Ok(if value.as_bool().context("Boolean variable required")? {
                    "true"
                } else {
                    "false"
                }
                .into())
            },
            VariableKind::Choice => {
                let text =
                    value.as_str().context("Choice variable required")?;
                ensure!(
                    self.choices.iter().any(|choice| choice == text),
                    "Unknown choice"
                );
                Ok(escape_text(text))
            },
        }
    }
}

impl Template {
    /// Binds every byte and its visibility metadata to an immutable revision.
    pub fn seal(&mut self) -> Result<()> {
        self.validate()?;
        self.assets.sort_by(|a, b| a.path.cmp(&b.path));
        self.version.clear();
        self.version =
            format!("{:x}", Sha256::digest(serde_json::to_vec(self)?));
        Ok(())
    }

    pub fn validate(&self) -> Result<()> {
        validate_id(&self.id, 32)?;
        ensure!(
            !self.name.trim().is_empty() &&
                self.name.len() <= 256 &&
                self.description.len() <= 4096 &&
                !self.owner.is_empty() &&
                self.owner.len() <= 256,
            "Invalid template metadata"
        );
        ensure!(
            match self.scope {
                Scope::Personal | Scope::Tenant =>
                    self.project.is_none() && self.plugin.is_none(),
                Scope::Project =>
                    self.project
                        .as_ref()
                        .is_some_and(|id| validate_id(id, 32).is_ok()) &&
                        self.plugin.is_none(),
                Scope::Plugin =>
                    self.project.is_none() &&
                        self.plugin.as_ref().is_some_and(|id| !id
                            .is_empty() &&
                            id.len() <= 64 &&
                            id.bytes()
                                .all(|c| c.is_ascii_alphanumeric() ||
                                    c == b'-' ||
                                    c == b'_')),
            },
            "Inconsistent template scope"
        );
        ensure!(
            !self.assets.is_empty() &&
                self.assets.len() <= 64 &&
                self.variables.len() <= 64,
            "Template cardinality exceeds limit"
        );
        validate_path(&self.entrypoint)?;
        ensure!(self.entrypoint.ends_with(".tex"), "Entrypoint must be TeX");
        let mut paths = BTreeSet::new();
        let mut size = 0_usize;
        for asset in &self.assets {
            validate_path(&asset.path)?;
            ensure!(paths.insert(&asset.path), "Duplicate asset path");
            size = size.saturating_add(asset.data.len());
            ensure!(size <= MAX_PROJECT_BYTES, "Project exceeds limit");
        }
        ensure!(paths.contains(&self.entrypoint), "Missing entrypoint");
        for (name, variable) in &self.variables {
            ensure!(
                !name.is_empty() &&
                    name.len() <= 64 &&
                    name.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || c == b'_'),
                "Invalid variable name"
            );
            ensure!(
                variable.choices.len() <= 64 &&
                    variable.choices.iter().all(|s| s.len() <= 256),
                "Invalid variable choices"
            );
        }
        Ok(())
    }

    /// Substitutes only original placeholders, never placeholders inside
    /// values.
    pub fn render(&self, document: &Document) -> Result<Rendered> {
        self.validate()?;
        ensure!(
            document
                .variables
                .keys()
                .all(|key| self.variables.contains_key(key)),
            "Unknown variable"
        );
        let mut values = BTreeMap::new();
        for (name, variable) in &self.variables {
            let value = match document.variables.get(name) {
                Some(value) => variable.render(value)?,
                None if variable.required => {
                    bail!("Missing required variable")
                },
                None => String::new(),
            };
            values.insert(name.as_str(), value);
        }
        let original = self
            .assets
            .iter()
            .find(|asset| asset.path == self.entrypoint)
            .context("Missing entrypoint")?;
        let source = document
            .source
            .as_deref()
            .map(str::as_bytes)
            .unwrap_or(&original.data);
        ensure!(source.len() <= MAX_PROJECT_BYTES, "Source exceeds limit");
        let render = |source: &[u8]| -> Result<Vec<u8>> {
            let text =
                std::str::from_utf8(source).context("TeX must be UTF-8")?;
            let mut result = String::with_capacity(text.len());
            let mut remainder = text;
            while let Some((prefix, suffix)) =
                remainder.split_once("<<galadril:")
            {
                result.push_str(prefix);
                let (name, rest) = suffix
                    .split_once(">>")
                    .context("Unclosed variable placeholder")?;
                result.push_str(
                    values
                        .get(name)
                        .context("Unknown template placeholder")?,
                );
                ensure!(
                    result.len() <= MAX_PROJECT_BYTES,
                    "Rendered source exceeds limit"
                );
                remainder = rest;
            }
            result.push_str(remainder);
            ensure!(
                result.len() <= MAX_PROJECT_BYTES,
                "Rendered source exceeds limit"
            );
            Ok(result.into_bytes())
        };
        let entrypoint = render(source)?;
        let mut assets = Vec::with_capacity(self.assets.len());
        let mut total = entrypoint.len();
        for asset in self
            .assets
            .iter()
            .filter(|asset| asset.path != self.entrypoint)
        {
            let data = if matches!(
                asset.path.rsplit_once('.').map(|(_, e)| e),
                Some("tex" | "cls" | "sty")
            ) {
                render(&asset.data)?
            } else {
                asset.data.clone()
            };
            total = total.saturating_add(data.len());
            ensure!(
                total <= MAX_PROJECT_BYTES,
                "Rendered project exceeds limit"
            );
            assets.push(Asset {
                path: asset.path.clone(),
                data,
            });
        }
        Ok(Rendered { entrypoint, assets })
    }
}
