//! GraphQL execution context tracking request invariants and domain ports.

use std::sync::Arc;

use crate::adapters::outbound::storage::s3::S3Uploader;
use crate::application::usecases::authorization::{AuthService, QueryContext};
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::search::SearchService;
use crate::config::AppConfig;

/// The context shared across all GraphQL resolvers.
pub struct AppContext {
    pub user_id: String,
    pub tenant_id: String,
    pub authn_issuer: Option<String>,
    pub authz_context: QueryContext,
    pub config: Arc<AppConfig>,
    pub identity: Arc<IdentityService>,
    pub iam_admin: Arc<IamAdminService>,
    pub explore: Arc<ExploreService>,
    pub search: Arc<SearchService>,
    pub auth_service: Arc<AuthService>,
    pub s3: Arc<S3Uploader>,
}

impl juniper::Context for AppContext {}
