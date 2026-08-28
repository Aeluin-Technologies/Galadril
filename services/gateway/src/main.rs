//! Gateway API for Galadril.

mod adapters;
mod application;
mod config;
mod domain;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
use loth::engine::{EngineSettings, LothEngine};
use loth::replication::ReplicationSettings;
use loth::spicedb::schema::SchemaMode;
use loth::types::{LothConfig, TextSource};
use secrecy::ExposeSecret;
use tokio::net::TcpListener;

use crate::adapters::inbound::graphql::auth::JwtRuntime;
use crate::adapters::inbound::graphql::server::create_router;
use crate::adapters::outbound::database::audit::PgAuditStore;
use crate::adapters::outbound::database::connection::Database;
use crate::adapters::outbound::database::entity_states::PgEntityStateStore;
use crate::adapters::outbound::database::iam::PgIamStore;
use crate::adapters::outbound::database::relations_age::PgAgeRelationsStore;
use crate::adapters::outbound::database::search::PgSearchStore;
use crate::adapters::outbound::database::user_directory::PgUserDirectory;
use crate::adapters::outbound::embedding::text::FakeEmbeddingGenerator;
use crate::adapters::outbound::storage::s3::S3Uploader;
use crate::application::usecases::audit::AuditService;
use crate::application::usecases::authorization::{
    AuthService, Authorization, GaladrilAuthContext,
};
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::search::SearchService;
use crate::config::AppConfig;

#[tokio::main]
async fn main() -> Result<()> {
    let telemetry = TelemetryConfig::Binary {
        name: "galadril-gateway",
        version: env!("CARGO_PKG_VERSION"),
    }
    .configure()?;

    let result = async {
        let config =
            Arc::new(AppConfig::load().context("Failed to load AppConfig")?);
        let _cache_ttl = Duration::from_mins(5);

        let database_url = config
            .database_url()
            .context("Failed to build database URL")?;
        let bind_addr = config.server.bind_addr();

        tracing::info!(
            event.name = "db.connection.starting",
            host = config.database.host,
            port = config.database.port,
            "connecting to database"
        );
        let database = Database::connect(&database_url)
            .await
            .context("Failed to initialize database connection pool")?;
        database
            .verify_security()
            .await
            .context("Unsafe application database role or RLS state")?;

        let iam_store = Arc::new(PgIamStore::new(database.clone()));
        let iam_store_dyn = Arc::clone(&iam_store)
            as Arc<dyn crate::application::ports::iam_store::IamStore>;

        let jwt = Arc::new(JwtRuntime::from_config(&config).map_err(|e| {
            anyhow::anyhow!("Failed to initialize JWT runtime: {e:?}")
        })?);

        let spicedb_endpoint =
            config.auth.spicedb_endpoint.as_deref().context(
                "Missing auth.spicedb_endpoint (or SPICEDB_ENDPOINT)",
            )?;
        let spicedb_token = config
            .auth
            .spicedb_token
            .as_ref()
            .context("Missing auth.spicedb_token (or SPICEDB_TOKEN)")?
            .expose_secret();

        let schema_path = std::env::var("SPICEDB_SCHEMA_PATH")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| {
                let local =
                    std::path::PathBuf::from("schemas/spicedb/schema.zed");
                if local.exists() {
                    local
                } else {
                    std::path::PathBuf::from("/schemas/spicedb/schema.zed")
                }
            });
        let cfg = LothConfig::new(
            spicedb_endpoint.to_string(),
            spicedb_token.to_string(),
        )
        .with_zed_schema(TextSource::from_path(schema_path));

        let settings = EngineSettings {
            schema_mode: SchemaMode::VerifyOnly,
            enable_replication_fail_closed: true,
        };

        let (engine, client) = LothEngine::from_config(cfg, settings)
            .await
            .context("Failed to initialize LothEngine")?;

        let (handle, worker) = engine.create_replication(
            Arc::clone(&client),
            4096,
            ReplicationSettings {
                max_batch: 256,
                flush_interval: Duration::from_millis(5),
                max_retries: 12,
                base_backoff: Duration::from_millis(25),
            },
        );

        let engine = engine.with_replication_fail_closed(handle.fatal_rx());

        tokio::spawn(async move {
            if let Err(e) = worker.run().await {
                tracing::error!(
                    event.name = "auth.replication.failed",
                    error = %e,
                    "authorization replication worker failed"
                );
            }
        });

        let replication_queue = handle.queue();
        let loth = Arc::new(engine);
        let auth_service = Arc::new(AuthService::new(
            loth,
            replication_queue,
            GaladrilAuthContext,
            Arc::clone(&iam_store_dyn),
        ));
        let authorization =
            Arc::clone(&auth_service) as Arc<dyn Authorization>;

        if cfg!(debug_assertions) {
            use crate::adapters::outbound::database::bootstrap::{
                provision_debug_admin, provision_debug_fixtures,
            };
            match provision_debug_admin(&database, &config).await {
                Ok(Some(p)) => {
                    tracing::info!(
                        event.name = "debug.admin.provisioned",
                        tenant_id = %p.tenant_id,
                        user_id = %p.user_id,
                        "debug administrator provisioned"
                    );

                    if let Err(e) = provision_debug_fixtures(
                        &database,
                        &auth_service,
                        "debug_tenant",
                        "admin",
                    )
                    .await
                    {
                        tracing::warn!(
                            event.name = "debug.fixtures.failed",
                            error = %e,
                            "debug fixture provisioning failed"
                        );
                    }
                },
                Ok(None) => {},
                Err(e) => {
                    tracing::warn!(
                        event.name = "debug.admin.failed",
                        error = %e,
                        "debug administrator provisioning failed"
                    )
                },
            }
        }

        let user_directory = Arc::new(PgUserDirectory::new(database.clone()));
        let identity = Arc::new(IdentityService::new(user_directory));

        let audit_store = Arc::new(PgAuditStore::new(database.clone()));
        let audit = Arc::new(AuditService::new(audit_store));

        let state_store = Arc::new(PgEntityStateStore::new(database.clone()));
        let relations_store =
            Arc::new(PgAgeRelationsStore::new(database.clone()));

        let explore = Arc::new(ExploreService::new(
            state_store.clone(),
            relations_store,
            Arc::clone(&authorization),
            "galadril_graph",
        ));

        let iam_admin = Arc::new(IamAdminService::new(
            Arc::clone(&iam_store_dyn),
            Arc::clone(&identity),
            Arc::clone(&authorization),
            Arc::clone(&audit),
        ));

        let search_store = Arc::new(PgSearchStore::new(database));
        let embedder = Arc::new(FakeEmbeddingGenerator::new());
        let search = Arc::new(SearchService::new(
            state_store,
            search_store,
            embedder,
            Arc::clone(&authorization),
        ));

        let s3 = {
            let cfg = config
                .s3
                .as_ref()
                .context("Missing connectors.s3 for uploads")?;

            Arc::new(
                S3Uploader::new(
                    &cfg.endpoint,
                    &cfg.bucket,
                    &cfg.bucket,
                    &cfg.region,
                    &cfg.access_key,
                    &cfg.secret_key,
                )
                .await?,
            )
        };

        let app = create_router(
            config,
            jwt,
            identity,
            iam_admin,
            explore,
            search,
            auth_service,
            s3,
        );

        tracing::info!(
            event.name = "http.server.listening",
            %bind_addr,
            "graphql api listening"
        );

        let listener = TcpListener::bind(bind_addr)
            .await
            .context("Failed to bind TCP listener")?;

        axum::serve(listener, app)
            .await
            .context("Server encountered a fatal error")?;

        Ok(())
    }
    .await;
    let shutdown_result = telemetry.shutdown();
    result.and(shutdown_result)
}
