//! Galadril bronze layer for data ingestion.
#![deny(unsafe_code, missing_docs)]
#![allow(dead_code)]

mod adapters;
mod application;
mod domain;

use std::net::SocketAddr;
use std::sync::Arc;

use tokio::net::TcpListener;
use tracing_subscriber::prelude::*;
use tracing_subscriber::{EnvFilter, fmt};

use crate::adapters::api::http::router as http_router;
use crate::adapters::spi::kafka::{
    KafkaConsumerAdapter, KafkaProducerAdapter,
};
use crate::adapters::spi::storage::S3Adapter;
use crate::application::IngestionService;
use crate::domain::authz::AuthzService;
use crate::domain::config::AppConfig;
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

    let config = AppConfig::from_env()?;
    tracing::info!(name = %config.pipeline.name, "pipeline configuration loaded");

    let s3_adapter: Arc<dyn BlobStorage> = Arc::new(
        S3Adapter::new(&config.s3_endpoint, &config.s3_bucket).await?,
    );

    let kafka_producer = Arc::new(
        KafkaProducerAdapter::new(
            &config.kafka_brokers,
            &config.schema_registry,
            &config.pipeline.sources,
        )
        .await?,
    );

    let ingestion_service = Arc::new(IngestionService::new(
        Arc::clone(&s3_adapter),
        kafka_producer,
        config.pipeline.clone(),
    ));

    // Optional HTTP server: enabled only when host is set.
    if let Some(host) = config.http_host.as_deref() {
        let bind: SocketAddr =
            format!("{}:{}", host, config.http_port).parse()?;

        let jwt = Arc::new(
            JwtRuntime::from_config(&config)
                .expect("cannot load JWT configuration"),
        );

        let authz = Arc::new(
            AuthzService::new(
                config
                    .spicedb_endpoint
                    .as_deref()
                    .expect("validated when http enabled"),
                config
                    .spicedb_token
                    .as_deref()
                    .expect("validated when http enabled"),
                config.cedar_policy_dsl.as_deref(),
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
        tracing::info!("intake_http_api_disabled (no INTAKE_HTTP_HOST)");
    }

    let consumer = KafkaConsumerAdapter::new(
        &config.kafka_brokers,
        &config.kafka_consumer_group,
        &config.kafka_notification_topic,
        ingestion_service,
    )
    .await?;

    tracing::info!("galadril intake service ready");
    consumer.run().await
}
