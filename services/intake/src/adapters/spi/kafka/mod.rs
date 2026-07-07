//! Base primitives for local/remote Kafka actions.

mod consumer;
mod producer;

use std::time::Duration;

use anyhow::{Context, Result, anyhow};
pub use consumer::*;
pub use producer::*;
use rdkafka::ClientConfig;
use rdkafka::admin::{AdminClient, AdminOptions, NewTopic};
use rdkafka::types::RDKafkaErrorCode;

/// Triggers direct workspace allocations for missing broker queues.
pub(super) async fn create_topics(
    config: &ClientConfig,
    topic_name: &str,
) -> Result<()> {
    let admin_client: AdminClient<_> =
        config.create().context("Failed to create AdminClient")?;
    let new_topic = NewTopic::new(
        topic_name,
        1,
        rdkafka::admin::TopicReplication::Fixed(1),
    );
    let options =
        AdminOptions::new().operation_timeout(Some(Duration::from_secs(5)));

    match admin_client.create_topics(&[new_topic], &options).await {
        Ok(results) => {
            for result in results {
                match result {
                    Ok(topic) => tracing::info!(?topic, "kafka topic created"),
                    Err((_, RDKafkaErrorCode::TopicAlreadyExists)) => {},
                    Err((topic, err)) => {
                        tracing::error!(
                            ?err,
                            ?topic,
                            "kafka topic assertion failed"
                        );
                        return Err(anyhow!(
                            "Failed to secure topic {topic}: {err:?}"
                        ));
                    },
                }
            }
        },
        Err(err) => return Err(anyhow!("Admin connection failure: {err:?}")),
    }

    Ok(())
}
