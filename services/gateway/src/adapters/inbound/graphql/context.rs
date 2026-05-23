//! GraphQL context holding application services and user state.

use std::sync::Arc;

use crate::application::usecases::data_explorer::DataExplorerService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::config::AppConfig;

/// The context shared across all GraphQL resolvers.
pub struct AppContext {
    /// The authenticated user's ID.
    pub user_id: String,
    /// The authenticated tenant ID (multi-tenant isolation boundary).
    pub tenant_id: String,
    /// Global immutable configuration.
    pub config: Arc<AppConfig>,
    /// Verifies the user exists/is-active and belongs to the tenant.
    pub identity: Arc<IdentityService>,
    pub data_explorer: Arc<DataExplorerService>,
    /// IAM administration (Cedar-gated, anti-escalation).
    pub iam_admin: Arc<IamAdminService>,
    /// Search + graph relations exploration (permission-filtered).
    pub explore: Arc<ExploreService>,
}

impl juniper::Context for AppContext {}
