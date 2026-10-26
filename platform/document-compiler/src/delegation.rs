//! Delegations resolve through Gateway's existing live generation capability.
use serde::{Deserialize, Serialize};

use crate::domain::Scope;

#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Resolve,
    Create,
    View,
    Use,
    Edit,
    Publish,
    Delete,
    PublishPdf,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TemplatePolicy {
    pub id: String,
    pub scope: Scope,
    pub owner: String,
    pub project: Option<String>,
    pub plugin: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AuthorizationRequest {
    pub action: Action,
    pub template: Option<TemplatePolicy>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    pub tenant: String,
    pub actor: String,
    pub generation: String,
    pub project: Option<String>,
    pub temporary_session: Option<String>,
}

impl From<&crate::domain::Template> for TemplatePolicy {
    fn from(value: &crate::domain::Template) -> Self {
        Self {
            id: value.id.clone(),
            scope: value.scope,
            owner: value.owner.clone(),
            project: value.project.clone(),
            plugin: value.plugin.clone(),
        }
    }
}
