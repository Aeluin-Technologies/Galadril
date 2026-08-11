//! Galadril bronze layer for data ingestion.
#![deny(unsafe_code, missing_docs)]
#![allow(dead_code)]

mod adapters;
mod application;
mod config;
mod domain;
mod telemetry;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use std::sync::Arc;

use anyhow::Context;

use crate::adapters::spi::kafka::{
    KafkaConsumerAdapter, KafkaProducerAdapter,
};
use crate::adapters::spi::storage::S3Adapter;
use crate::application::IngestionService;
use crate::application::router::PipelineRouter;
use crate::config::AppConfig;
use crate::domain::ports::{BlobStorage, EventProducer};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let tracer_provider = telemetry::initialize("galadril-intake")?;

    let config = AppConfig::load()?;
    tracing::info!("bootstrap infrastructure configuration loaded");

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

    let raw_schemas = crate::application::pipeline::discover_local_schemas(
        "/schemas",
    )
    .await
    .context(
        "failed to perform async scan over local /schemas target storage",
    )?;

    let kafka_producer = KafkaProducerAdapter::new(
        &config.kafka.brokers,
        &config.kafka.schema_registry,
        raw_schemas,
    )
    .await?;

    let event_producer: Arc<dyn EventProducer> = Arc::new(kafka_producer);
    let pipeline_router =
        Arc::new(PipelineRouter::new(Arc::clone(&s3_adapter), 10_000));

    let ingestion_service = Arc::new(IngestionService::new(
        Arc::clone(&s3_adapter),
        Arc::clone(&event_producer),
        pipeline_router,
    ));

    let consumer = KafkaConsumerAdapter::new(
        &config.kafka.brokers,
        &config.kafka.consumer_group,
        &config.s3.bucket_notifications,
        ingestion_service,
    )
    .await?;

    tracing::info!("galadril intake service ready");
    let result = consumer.run().await;
    if let Err(error) = tracer_provider.shutdown() {
        tracing::error!(?error, "failed to shut down intake telemetry");
    }
    result
}
