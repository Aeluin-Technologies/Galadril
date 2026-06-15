//! SpiceDB authorization via Loth (mirrors gateway usage for
//! check_permission).

use std::sync::Arc;

use anyhow::{Context, Result};
use loth::engine::{EngineSettings, LothEngine};
use loth::spicedb::schema::SchemaMode;
use loth::types::{
    AuthError as LothAuthError, CedarContext, CedarContextBuilder, LothConfig,
    TextSource,
};

/// Permissions aligned with gateway usage. For intake uploads we use "write".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Permission {
    Write,
}

impl Permission {
    pub fn as_str(self) -> &'static str {
        match self {
            Permission::Write => "write",
        }
    }
}

/// Custom Cedar context placeholder (kept for parity with gateway).
#[derive(Debug, Default, Clone)]
pub struct GaladrilAuthContext;

impl<'a> CedarContext<'a> for GaladrilAuthContext {
    fn write_to(
        &self,
        _out: &mut CedarContextBuilder<'a>,
    ) -> Result<(), LothAuthError> {
        Ok(())
    }
}

/// Intake authorization service.
pub struct AuthzService {
    loth: Arc<LothEngine>,
    default_ctx: GaladrilAuthContext,
}

impl AuthzService {
    /// Creates an Authz service by initializing a Loth engine.
    pub async fn new(
        spicedb_endpoint: &str,
        spicedb_token: &str,
        cedar_policy_dsl_path: Option<&str>,
    ) -> Result<Self> {
        let mut cfg = LothConfig::new(
            spicedb_endpoint.to_string(),
            spicedb_token.to_string(),
        );

        if let Some(path) = cedar_policy_dsl_path {
            cfg = cfg.with_cedar_policies(TextSource::from_path(path));
        }

        let settings = EngineSettings {
            schema_mode: SchemaMode::ApplyIfDifferent,
            enable_replication_fail_closed: true,
        };

        let (engine, _client) = LothEngine::from_config(cfg, settings)
            .await
            .context("Failed to initialize LothEngine")?;

        Ok(Self {
            loth: Arc::new(engine),
            default_ctx: GaladrilAuthContext,
        })
    }

    /// Checks if `principal` has `permission` for `resource_type:resource_id`.
    pub async fn is_authorized(
        &self,
        principal: &str,
        permission: Permission,
        resource_type: &str,
        resource_id: &str,
    ) -> Result<bool> {
        let rid = resource_id.trim();

        self.loth
            .prepare_check(principal, permission.as_str(), resource_type, rid)
            .with_context(&self.default_ctx)
            .check()
            .await
            .context("Loth check_permission failed")
    }
}
