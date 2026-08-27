//! GraphQL execution context tracking request invariants and domain ports.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};

use crate::application::usecases::authorization::QueryContext;
use crate::application::usecases::control_plane::ControlPlaneService;
use crate::application::usecases::conversations::ConversationService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::pipelines::PipelineService;
use crate::application::usecases::search::SearchService;
use crate::application::usecases::uploads::UploadService;

/// The context shared across all GraphQL resolvers.
pub struct AppContext {
    pub user_id: String,
    pub tenant_id: String,
    pub authn_issuer: Option<String>,
    pub authn_expires_at: usize,
    pub authz_context: QueryContext,
    pub control_plane: Arc<ControlPlaneService>,
    pub conversations: Arc<ConversationService>,
    pub pipelines: Arc<PipelineService>,
    pub identity: Arc<IdentityService>,
    pub iam_admin: Arc<IamAdminService>,
    pub explore: Arc<ExploreService>,
    pub search: Arc<SearchService>,
    pub uploads: Arc<UploadService>,
}

impl AppContext {
    /// Rejects work started after the JWT used for this connection expired.
    pub fn verify_authentication(&self) -> Result<()> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .context("System clock is before the Unix epoch")?;
        verify_expiration(self.authn_expires_at, now.as_secs())
    }
}

/// Compares signed JWT expiration with an explicit clock for deterministic
/// tests.
fn verify_expiration(expires_at: usize, now: u64) -> Result<()> {
    let expires_at = u64::try_from(expires_at)
        .context("JWT expiration does not fit the platform clock")?;
    if expires_at <= now {
        bail!("Authentication expired");
    }
    Ok(())
}

impl juniper::Context for AppContext {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn websocket_authentication_expires_fail_closed() {
        assert!(verify_expiration(101, 100).is_ok());
        assert!(verify_expiration(100, 100).is_err());
        assert!(verify_expiration(99, 100).is_err());
    }
}
