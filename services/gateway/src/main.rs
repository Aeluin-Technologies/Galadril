//! Gateway API for Galadril.

mod adapters;
mod application;
mod authentication;
mod config;
mod domain;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
use loth::engine::{EngineSettings, LothEngine};
use loth::spicedb::schema::SchemaMode;
use loth::types::{LothConfig, TextSource};
use secrecy::{ExposeSecret, SecretString};
use tokio::net::TcpListener;

use crate::adapters::inbound::graphql::server::{
    GatewayServices, create_router,
};
use crate::adapters::outbound::access::SpiceDbAccessStore;
use crate::adapters::outbound::database::audit::PgAuditStore;
use crate::adapters::outbound::database::connection::Database;
use crate::adapters::outbound::database::conversations::PgConversationStore;
use crate::adapters::outbound::database::entity_states::PgEntityStateStore;
use crate::adapters::outbound::database::federation::PgFederationStore;
use crate::adapters::outbound::database::iam::PgIamStore;
use crate::adapters::outbound::database::relations_age::PgAgeRelationsStore;
use crate::adapters::outbound::database::search::PgSearchStore;
use crate::adapters::outbound::database::sso::PgSsoStore;
use crate::adapters::outbound::database::user_directory::PgUserDirectory;
use crate::adapters::outbound::embedding::text::FakeEmbeddingGenerator;
use crate::adapters::outbound::polis::PolisBroker;
use crate::adapters::outbound::registry::RegistryStore;
use crate::adapters::outbound::scribe::{DisabledScribeAgent, ScribeAgent};
use crate::adapters::outbound::storage::s3::S3Uploader;
use crate::application::ports::conversation_agent::ConversationAgent;
use crate::application::usecases::audit::AuditService;
use crate::application::usecases::authorization::{
    AuthService, Authorization, GaladrilAuthContext,
};
use crate::application::usecases::control_plane::ControlPlaneService;
use crate::application::usecases::conversations::ConversationService;
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::iam_admin::IamAdminService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::pipelines::PipelineService;
use crate::application::usecases::search::SearchService;
use crate::application::usecases::sso_admin::SsoAdminService;
use crate::application::usecases::uploads::UploadService;
use crate::config::AppConfig;

/// Starts the authenticated, tenant-scoped GraphQL control plane.
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
        let chatgpt_callback = if std::env::var("PENGOLO_CHATGPT_ENABLED").is_ok_and(|value| value == "true") {
            anyhow::ensure!(config.scribe.enabled, "ChatGPT plan usage requires Scribe");
            Some(crate::domain::chatgpt::CallbackConfig::parse(&std::env::var("PENGOLO_CHATGPT_CALLBACK_BIND").unwrap_or_else(|_| "127.0.0.1:1455".into()))?)
        } else { None };

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
            enable_replication_fail_closed: false,
        };

        let (engine, _client) = LothEngine::from_config(cfg, settings)
            .await
            .context("Failed to initialize LothEngine")?;
        let loth = Arc::new(engine);
        let auth_service = Arc::new(AuthService::new(
            loth,
            GaladrilAuthContext,
            Arc::clone(&iam_store_dyn),
        ));
        let authorization =
            Arc::clone(&auth_service) as Arc<dyn Authorization>;

        if cfg!(debug_assertions) {
            use crate::adapters::outbound::database::bootstrap::{
                provision_debug_admin, provision_debug_fixtures,
            };
            match provision_debug_admin(&database).await {
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
                        &config.database.graph_name,
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
            config.database.graph_name.clone(),
        ));

        let access_store = Arc::new(
            SpiceDbAccessStore::connect(spicedb_endpoint, spicedb_token).await?,
        );
        let iam_admin = Arc::new(
            IamAdminService::new(
                Arc::clone(&iam_store_dyn),
                Arc::clone(&identity),
                Arc::clone(&authorization),
                Arc::clone(&audit),
            )
            .with_access_store(access_store.clone()),
        );

        let registry_client = config
            .registry
            .as_ref()
            .context("registry.endpoint is required")?
            .connect_lazy()?;
        let sso_admin = match std::env::var("SSO_POLIS_API_KEY") {
            Ok(key) => {
                anyhow::ensure!(
                    key.len() >= 32,
                    "SSO_POLIS_API_KEY requires at least 32 random characters"
                );
                let broker = PolisBroker::new(
                    &std::env::var("SSO_POLIS_URL").context("Missing SSO_POLIS_URL")?,
                    SecretString::from(key),
                    &std::env::var("AUTH_PUBLIC_ORIGIN").context("Missing AUTH_PUBLIC_ORIGIN")?,
                    registry_client.clone(),
                )?;
                Some(Arc::new(
                    SsoAdminService::new(
                        Arc::new(PgSsoStore::new(database.clone())),
                        Arc::new(broker),
                        Arc::clone(&iam_admin),
                        Arc::clone(&authorization),
                        Arc::clone(&audit),
                    )
                    .with_federation_store(Arc::new(PgFederationStore::new(
                        database.clone(),
                    ))),
                ))
            },
            Err(std::env::VarError::NotPresent) => None,
            Err(error) => return Err(error.into()),
        };
        let provisioning_key = match std::env::var("SSO_PROVISIONING_KEY") {
            Ok(key) => {
                anyhow::ensure!(
                    key.len() >= 32,
                    "SSO_PROVISIONING_KEY requires at least 32 random characters"
                );
                Some(SecretString::from(key))
            },
            Err(std::env::VarError::NotPresent) => None,
            Err(error) => return Err(error.into()),
        };
        let registry_store =
            Arc::new(RegistryStore::new(registry_client, database.clone()));
        let control_plane_store = registry_store.clone();
        let control_plane = Arc::new(ControlPlaneService::new(
            control_plane_store,
            Arc::clone(&identity),
            Arc::clone(&authorization),
            Arc::clone(&audit),
        ));

        let search_store = Arc::new(PgSearchStore::new(database.clone()));
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
                    cfg.public_endpoint.as_deref().or(std::env::var("AUTH_PUBLIC_ORIGIN").ok().as_deref()),
                    &cfg.staging_bucket,
                    &cfg.bucket,
                    &cfg.region,
                    &cfg.access_key,
                    &cfg.secret_key,
                )
                .await?,
            )
        };
        let uploads = Arc::new(UploadService::new(
            s3.clone(),
            Arc::clone(&identity),
            Arc::clone(&authorization),
            Arc::clone(&audit),
        ));

        let pipeline_store = registry_store;
        let causal = Arc::new(crate::application::usecases::causal::CausalService::new(
            Arc::new(crate::adapters::outbound::database::causal::PgCausalStore::new(database.clone())),
            Arc::clone(&authorization),
        ));
        let pengolo_store = Arc::new(crate::adapters::outbound::database::pengolo::PgPengoloStore::new(database.clone()));
        let document_index=Arc::new(crate::adapters::outbound::database::document_index::PgDocumentIndex::new(database.clone()));
        let conversation_store = Arc::new(PgConversationStore::new(database));
        let chat_tools = Arc::new(
            crate::application::usecases::chat_tools::ChatTools::new(
                Arc::clone(&identity),
                Arc::clone(&authorization),
                Arc::clone(&audit),
                Arc::clone(&search),
                Arc::clone(&explore),
                causal,
                conversation_store.clone(),
            ),
        );
        let scribe: Arc<dyn ConversationAgent> = if config.scribe.enabled {
            ScribeAgent::new(&config.scribe, Arc::clone(&chat_tools))
                .context("Failed to initialize Scribe")?
        } else {
            Arc::new(DisabledScribeAgent)
        };
        let pipelines = Arc::new(PipelineService::new(
            pipeline_store, Arc::clone(&identity), Arc::clone(&authorization), Arc::clone(&audit),
        ).with_runtime_catalog(Arc::clone(&scribe)));
        let documents=Arc::new(crate::application::usecases::documents::DocumentRetrieval::new(document_index,Arc::clone(&scribe),s3.clone(),Arc::clone(&authorization)));
        if config.scribe.enabled {
            let worker=Arc::clone(&documents);
            std::mem::drop(tokio::spawn(async move {
                let mut interval=tokio::time::interval(std::time::Duration::from_secs(5));
                interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                loop { interval.tick().await; if worker.tick().await.is_err() {tracing::warn!(event.name="document.index.worker_deferred");} }
            }));
        }
        let pengolo = Arc::new(crate::application::usecases::pengolo::PengoloService::new(
            pengolo_store, Arc::clone(&scribe), s3.clone(), Arc::clone(&identity), Arc::clone(&authorization), Arc::clone(&iam_store_dyn), access_store,
        ).with_documents(documents).with_chatgpt(chatgpt_callback.as_ref().map(|callback| callback.redirect.clone())));
        chat_tools.set_pengolo(Arc::downgrade(&pengolo))?;
        let maintenance=Arc::clone(&pengolo);
        std::mem::drop(tokio::spawn(async move {
            let mut interval=tokio::time::interval(std::time::Duration::from_secs(60));
            loop {interval.tick().await;if maintenance.cleanup().await.is_err() {tracing::warn!(event.name="pengolo.cleanup.deferred");}}
        }));
        let conversations = Arc::new(ConversationService::new(
            conversation_store,
            scribe,
            s3.clone(),
            Arc::clone(&identity),
            Arc::clone(&authorization),
            Arc::clone(&audit),
        ));

        let services = Arc::new(GatewayServices {
            local_provisioning: std::env::var("AUTH_LOCAL_LOGIN_ENABLED").is_ok_and(|value| value == "true"),
            sso_admin,
            provisioning_key,
            chat_tools,
            identity,
            iam_admin,
            explore,
            search,
            control_plane,
            conversations,
            pengolo,
            pipelines,
            uploads,
        });
        let callback_service = Arc::clone(&services.pengolo);
        let mut app = create_router(services, &config.server);
        if let Ok(database_url) = std::env::var("AUTH_DATABASE_URL") {
            let key = SecretString::from(
                std::env::var("AUTH_GATEWAY_KEY").context("Missing AUTH_GATEWAY_KEY")?,
            );
            let memberships = Arc::new(
                adapters::outbound::federation_membership::FederationMembershipWriter(
                    Arc::clone(&authorization),
                ),
            );
            app = app.merge(
                authentication::router_with_membership(&database_url, key, memberships)
                    .await?,
            );
        }

        tracing::info!(
            event.name = "http.server.listening",
            %bind_addr,
            "graphql api listening"
        );

        let listener = TcpListener::bind(bind_addr)
            .await
            .context("Failed to bind TCP listener")?;

        if let Some(callback) = chatgpt_callback {
            let oauth_listener = TcpListener::bind(callback.bind).await.context("Failed to bind the personal ChatGPT callback")?;
            let callback_app = crate::adapters::inbound::pengolo::chatgpt_callback_router(callback_service);
            tokio::try_join!(
                async { axum::serve(listener, app).await },
                async { axum::serve(oauth_listener, callback_app).await },
            ).context("Server encountered a fatal error")?;
        } else {
            axum::serve(listener, app).await.context("Server encountered a fatal error")?;
        }

        Ok(())
    }
    .await;
    let shutdown_result = telemetry.shutdown();
    result.and(shutdown_result)
}
