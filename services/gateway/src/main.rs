//! Gateway API for Galadril.

mod adapters;
mod application;
mod domain;

use std::env;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use tokio::net::TcpListener;

use crate::adapters::inbound::graphql::server::create_router;
use crate::adapters::outbound::database::connection::create_pool;
use crate::adapters::outbound::database::data_inspector::PgDataIntrospector;
use crate::adapters::outbound::database::entity::PgAgeEntityProvider;
use crate::adapters::outbound::database::policy::PgPolicyStore;
use crate::adapters::outbound::database::user_directory::PgUserDirectory;
use crate::application::usecases::authorization::AuthService;
use crate::application::usecases::data_explorer::DataExplorerService;
use crate::application::usecases::identity::IdentityService;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let database_url = env::var("DATABASE_URL").unwrap_or_else(|_| {
        "postgres://postgres:postgres@localhost:5432/galadril_dev".to_string()
    });

    let port = env::var("PORT").unwrap_or_else(|_| "8080".to_string());
    let addr = format!("0.0.0.0:{}", port);

    let graph_name = env::var("GRAPH_NAME")
        .unwrap_or_else(|_| "galadril_graph".to_string());
    let cache_ttl = Duration::from_mins(5);

    tracing::info!(?database_url, "connecting database");
    let pool = create_pool(&database_url)
        .await
        .context("Failed to initialize database connection pool")?;

    let data_introspector = Arc::new(PgDataIntrospector::new(pool.clone()));
    let policy_store = Arc::new(PgPolicyStore::new(pool.clone()));
    let entity_provider =
        Arc::new(PgAgeEntityProvider::new(pool.clone(), &graph_name));

    let auth_service =
        Arc::new(AuthService::new(policy_store, entity_provider, cache_ttl));

    let data_explorer = Arc::new(DataExplorerService::new(
        data_introspector,
        auth_service,
        cache_ttl,
    ));

    let user_directory = Arc::new(PgUserDirectory::new(pool));
    let identity = Arc::new(IdentityService::new(user_directory));

    let app = create_router(identity, data_explorer);

    tracing::info!(?addr, "graphql api listening");
    tracing::info!("graphiql ide available");

    let listener = TcpListener::bind(&addr)
        .await
        .context("Failed to bind TCP listener")?;

    axum::serve(listener, app)
        .await
        .context("Server encountered a fatal error")?;

    Ok(())
}
