//! Galadril bronze layer for data ingestion.
#![deny(unsafe_code, missing_docs)]
#![allow(dead_code)]

mod adapters;
mod application;
mod config;
mod domain;

use std::sync::Arc;

use secrecy::ExposeSecret;
use tokio::net::TcpListener;
use tracing_subscriber::prelude::*;
use tracing_subscriber::{EnvFilter, fmt};

use crate::adapters::api::http::router as http_router;
use crate::adapters::spi::kafka::{
    KafkaConsumerAdapter, KafkaProducerAdapter,
};
use crate::adapters::spi::storage::S3Adapter;
use crate::application::IngestionService;
use crate::config::AppConfig;
use crate::domain::authz::AuthzService;
use crate::domain::jwt::JwtRuntime;
use crate::domain::ports::BlobStorage;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
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

    let config = AppConfig::load()?;
    tracing::info!(name = %config.pipeline.name, "pipeline configuration loaded");

    let s3_adapter: Arc<dyn BlobStorage> = Arc::new(
        S3Adapter::new(
            &config.s3.endpoint,
            &config.s3.bucket,
            &config.s3.region,
            &config.s3.access_key,
            &config.s3.secret_key,
        )
        .await?,
    );

    let kafka_producer = Arc::new(
        KafkaProducerAdapter::new(
            &config.kafka.brokers,
            &config.kafka.schema_registry,
            &config.pipeline.sources[..],
        )
        .await?,
    );

    let ingestion_service = Arc::new(IngestionService::new(
        Arc::clone(&s3_adapter),
        kafka_producer,
        config.pipeline.clone(),
    ));

    if let Some(bind) = config.server.bind_addr() {
        let jwt = Arc::new(JwtRuntime::from_config(&config).expect(
            "Failed to initialize cryptographic JWT validation runtime",
        ));

        let authz = Arc::new(
            AuthzService::new(
                config
                    .auth
                    .spicedb_endpoint
                    .as_deref()
                    .expect("Invariants validated via app initialization constraints"),
                config
                    .auth
                    .spicedb_token
                    .as_ref()
                    .expect("Invariants validated via app initialization constraints")
                    .expose_secret(),
                None,
            )
            .await?,
        );

        let app = http_router::create_router(jwt, authz, s3_adapter);

        tokio::spawn(async move {
            tracing::info!(%bind, "intake_http_api_listening");
            let listener = match TcpListener::bind(bind).await {
                Ok(l) => l,
                Err(e) => {
                    tracing::error!(error = %e, "intake_http_bind_failed");
                    return;
                },
            };

            if let Err(e) = axum::serve(listener, app).await {
                tracing::error!(error = %e, "intake_http_server_failed");
            }
        });
    } else {
        tracing::info!("http api disabled, host variable unset");
    }

    let consumer = KafkaConsumerAdapter::new(
        &config.kafka.brokers,
        &config.kafka.consumer_group,
        &config.s3.bucket_notifications,
        ingestion_service,
    )
    .await?;

    tracing::info!("galadril intake service ready");
    consumer.run().await
}
