//! IAM permission value objects.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Allow/Deny effect aligned with Cedar semantics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Effect {
    Allow,
    Deny,
}

/// A persisted permission record (user or role).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IamPermission {
    pub effect: Effect,
    pub action: String,
    /// Scope is an opaque JSON object but must be validated by the
    /// application.
    pub scope: Value,
}
