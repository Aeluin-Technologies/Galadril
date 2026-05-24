//! Gateway API for Galadril.

mod adapters;
mod application;
mod config;
mod domain;

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use tokio::net::TcpListener;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::{EnvFilter, fmt};

use crate::adapters::inbound::graphql::auth::JwtRuntime;
use crate::adapters::inbound::graphql::server::create_router;
use crate::adapters::outbound::database::bootstrap::run_migrations;
use crate::adapters::outbound::database::connection::create_pool;
use crate::adapters::outbound::database::data_inspector::PgDataIntrospector;
use crate::adapters::outbound::database::entity::PgAgeEntityProvider;
use crate::adapters::outbound::database::entity_states::PgEntityStateStore;
use crate::adapters::outbound::database::iam::PgIamStore;
use crate::adapters::outbound::database::policy::PgPolicyStore;
use crate::adapters::outbound::database::relations_age::PgAgeRelationsStore;
use crate::adapters::outbound::database::user_directory::PgUserDirectory;
use crate::application::usecases::authorization::AuthService;
use crate::application::usecases::data_explorer::DataExplorerService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::config::AppConfig;

#[tokio::main]
async fn main() -> Result<()> {
    let level = if cfg!(debug_assertions) {
        "debug"
    } else {
        "info"
    };
    tracing_subscriber::registry()
        .with(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new(level)),
        )
        .with(fmt::layer())
        .init();

    let config =
        Arc::new(AppConfig::load().context("Failed to load AppConfig")?);
    let cache_ttl = Duration::from_mins(5);
    let database_url = config
        .database_url()
        .context("Failed to build database URL")?;
    let addr = format!("0.0.0.0:{}", config.server.port);

    tracing::info!(port = config.server.port, "connecting database");
    let pool = create_pool(&database_url)
        .await
        .context("Failed to initialize database connection pool")?;

    run_migrations(&pool)
        .await
        .context("Failed to run database migrations")?;

    if cfg!(debug_assertions) {
        use crate::adapters::outbound::database::bootstrap::provision_debug_admin;
        match provision_debug_admin(&pool, &config).await {
            Ok(Some(p)) => {
                tracing::info!(
                    tenant_id = %p.tenant_id,
                    user_id = %p.user_id,
                    jwt = %p.jwt,
                    "debug_admin_provisioned"
                );
            },
            Ok(None) => {},
            Err(e) => {
                tracing::warn!(error = %e, "debug_admin_provision_failed");
            },
        }
    }

    let data_introspector = Arc::new(PgDataIntrospector::new(pool.clone()));
    let policy_store = Arc::new(PgPolicyStore::new(pool.clone()));
    let entity_provider =
        Arc::new(PgAgeEntityProvider::new(pool.clone(), "galadril_graph"));

    let auth_service =
        Arc::new(AuthService::new(policy_store, entity_provider, cache_ttl));

    let data_explorer = Arc::new(DataExplorerService::new(
        data_introspector,
        Arc::clone(&auth_service),
        cache_ttl,
    ));

    let user_directory = Arc::new(PgUserDirectory::new(pool.clone()));
    let identity = Arc::new(IdentityService::new(user_directory));

    let jwt = Arc::new(
        JwtRuntime::from_config(&config)
            .expect("Failed to initialize JWT runtime"),
    );

    let iam_store = Arc::new(PgIamStore::new(pool.clone()));
    let state_store = Arc::new(PgEntityStateStore::new(pool.clone()));
    let relations_store = Arc::new(PgAgeRelationsStore::new(pool.clone()));

    let explore = Arc::new(ExploreService::new(
        state_store,
        relations_store,
        Arc::clone(&auth_service),
        "galadril_graph",
    ));

    let iam_admin = Arc::new(IamAdminService::new(
        iam_store,
        Arc::clone(&identity),
        Arc::clone(&auth_service),
        Arc::clone(&data_explorer),
    ));

    let app = create_router(
        config,
        jwt,
        identity,
        data_explorer,
        iam_admin,
        explore,
    );

    tracing::info!(%addr, "graphql api listening");

    let listener = TcpListener::bind(&addr)
        .await
        .context("Failed to bind TCP listener")?;

    axum::serve(listener, app)
        .await
        .context("Server encountered a fatal error")?;

    Ok(())
}
