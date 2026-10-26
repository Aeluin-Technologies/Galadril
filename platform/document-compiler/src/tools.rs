//! Model-visible operations never carry principal or object-store credentials.
use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::domain::{Asset, Document, Scope, Variable};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TemplateDraft {
    pub id: Option<String>,
    pub name: String,
    pub description: String,
    pub scope: Scope,
    pub project: Option<String>,
    pub plugin: Option<String>,
    pub entrypoint: String,
    pub assets: Vec<Asset>,
    pub variables: BTreeMap<String, Variable>,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum TemplateOperation {
    PluginCatalog,
    EnablePlugin {
        plugin_id: String,
        enabled: bool,
    },
    Export {
        template_id: String,
        version: String,
        paths: Vec<String>,
        name: String,
    },
    List {
        #[serde(default)]
        after: String,
    },
    Read {
        template_id: String,
        version: String,
        path: Option<String>,
        #[serde(default)]
        offset: usize,
    },
    Put {
        template: TemplateDraft,
    },
    Publish {
        template_id: String,
        version: String,
    },
    Delete {
        template_id: String,
        version: String,
    },
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompileInput {
    pub template_id: String,
    pub version: String,
    pub idempotency_key: String,
    pub document: Document,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum JobOperation {
    SaveBatch { job_ids: Vec<String>, name: String },
    Status { job_id: String },
    Cancel { job_id: String },
    Save { job_id: String, name: String },
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PluginTemplate {
    pub plugin_id: String,
    pub plugin_version: String,
    pub key: String,
    pub template: TemplateDraft,
}

pub fn plugin_template_id(tenant: &str, plugin: &str, key: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = format!(
        "{:x}",
        Sha256::digest(format!("{tenant}/{plugin}/{key}").as_bytes())
    );
    let mut id = String::from("f");
    id.extend(digest.chars().take(31));
    id
}
