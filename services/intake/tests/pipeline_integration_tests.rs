//! Integration testing suite for the `intake` processing pipeline.

use std::sync::Arc;

use anyhow::Result;
use async_trait::async_trait;
use intake::application::IngestionService;
use intake::config::{
    AppConfig, AuthConfig, JwtConfig, KafkaConfig, S3Config, ServerConfig,
};
use intake::domain::models::{PipelineConfig, SourceConfig};
use intake::domain::ports::{
    AuthzHints, BlobStorage, EventProducer, IngestionServicePort,
};
use mockall::mock;
use serde_json::{Value, json};

mock! {
    /// Mock implementation of the BlobStorage port matching exact trait declaration bounds.
    pub BlobStorageMock {}

    #[async_trait]
    impl BlobStorage for BlobStorageMock {
        async fn upload_file(&self, file_name: &str, data: &[u8]) -> Result<String>;
        async fn upload_file_with_authz(&self, key: &str, data: &[u8], authz: &AuthzHints) -> Result<String>;
        async fn download_file(&self, key: &str) -> Result<Vec<u8>>;
        async fn authz_hints(&self, bucket: &str, key: &str) -> Result<AuthzHints>;
    }
}

mock! {
    /// Mock implementation of the EventProducer port matching exact trait declaration bounds.
    pub EventProducerMock {}

    #[async_trait]
    impl EventProducer for EventProducerMock {
        #[mockall::concretize]
        async fn publish(&self, topic: &str, schema_path: Option<&str>, key: &str, payload: &Value) -> Result<()>;
    }
}

/// Generates a comprehensive application configuration scenario.
///
/// This fixture simulates full system environments containing multi-tenant
/// pipelines, security profiles, and backend target definitions required by
/// the intake binary.
fn create_test_application_config() -> AppConfig {
    let pipeline_cfg = PipelineConfig {
        name: "galadril-bronze-intake-pipeline".to_string(),
        connectors: serde_json::from_value(
            json!({ "kafka": null, "s3": null }),
        )
        .unwrap(),
        sources: vec![
            SourceConfig {
                id: "financial-csv-source".to_string(),
                topic: "finance.bronze.transactions".to_string(),
                schema_path: Some("schemas/transaction.avsc".to_string()),
                match_pattern: Some("^[^/]+/finance/.*\\.csv$".to_string()),
                parser: "csv".to_string(),
            },
            SourceConfig {
                id: "identity-json-source".to_string(),
                topic: "iam.bronze.events".to_string(),
                schema_path: None,
                match_pattern: Some("^[^/]+/identity/.*\\.json$".to_string()),
                parser: "json".to_string(),
            },
        ],
    };

    AppConfig {
        server: ServerConfig {
            host: None,
            port: 8080,
        },
        kafka: KafkaConfig {
            brokers: "kafka-cluster:9092".to_string(),
            consumer_group: "intake-group".to_string(),
            schema_registry: "http://registry:8081".to_string(),
        },
        s3: S3Config {
            endpoint: "http://minio:9000".to_string(),
            region: "us-east-1".to_string(),
            bucket: "galadril-bronze".to_string(),
            bucket_notifications: "enabled".to_string(),
            access_key: "minioadmin".to_string(),
            secret_key: "minioadmin".to_string(),
        },
        jwt: JwtConfig {
            issuer: Some("https://auth.galadril.com".to_string()),
            audience: Some("intake-service".to_string()),
            es256_public_key_pem: Some(
                "-----BEGIN PUBLIC KEY-----\nMOCK...".to_string(),
            ),
        },
        auth: AuthConfig {
            spicedb_endpoint: Some("http://spicedb:50051".to_string()),
            spicedb_token: Some("secret_token".to_string().into()),
        },
        pipeline: pipeline_cfg,
    }
}

#[tokio::test]
async fn test_intake_pipeline_e2e_lifecycle() {
    let app_config = create_test_application_config();

    let mut storage_mock = MockBlobStorageMock::new();
    let mut producer_mock = MockEventProducerMock::new();

    let transaction_csv_data = b"id,amount,currency,status\ntx_201,99.99,EUR,settled\ntx_202,450.00,USD,pending";
    let identity_json_data = b"{\"user_id\": \"usr_abc123\", \"action\": \"login_attempt\", \"risk_score\": 0.12}";

    storage_mock
        .expect_authz_hints()
        .with(
            mockall::predicate::eq("galadril-bronze"),
            mockall::predicate::eq("tenant-alpha/finance/october_tx.csv"),
        )
        .times(1)
        .returning(|_, _| {
            Ok(AuthzHints {
                tenant: Some("tenant-alpha".to_string()),
                viewers: vec!["finance-auditors".to_string()],
                owner: Some("finance-manager".to_string()),
            })
        });

    storage_mock
        .expect_authz_hints()
        .with(
            mockall::predicate::eq("galadril-bronze"),
            mockall::predicate::eq("tenant-beta/identity/audit.json"),
        )
        .times(1)
        .returning(|_, _| {
            Ok(AuthzHints {
                tenant: Some("tenant-beta".to_string()),
                viewers: vec!["security-ops".to_string()],
                owner: Some("iam-admin".to_string()),
            })
        });

    storage_mock
        .expect_download_file()
        .with(mockall::predicate::eq(
            "tenant-alpha/finance/october_tx.csv",
        ))
        .times(1)
        .return_once(move |_| Ok(transaction_csv_data.to_vec()));

    storage_mock
        .expect_download_file()
        .with(mockall::predicate::eq("tenant-beta/identity/audit.json"))
        .times(1)
        .return_once(move |_| Ok(identity_json_data.to_vec()));

    producer_mock
        .expect_publish()
        .withf(
            |topic: &str,
             schema: &Option<&str>,
             key: &str,
             payload: &Value| {
                topic == "finance.bronze.transactions" &&
                    *schema == Some("schemas/transaction.avsc") &&
                    key == "tenant-alpha/finance/october_tx.csv" &&
                    payload["id"] == "tx_201" &&
                    payload["amount"] == 99.99 &&
                    payload["currency"] == "EUR"
            },
        )
        .times(1)
        .returning(|_, _, _, _| Ok(()));

    producer_mock
        .expect_publish()
        .withf(
            |topic: &str,
             schema: &Option<&str>,
             key: &str,
             payload: &Value| {
                topic == "finance.bronze.transactions" &&
                    *schema == Some("schemas/transaction.avsc") &&
                    key == "tenant-alpha/finance/october_tx.csv" &&
                    payload["id"] == "tx_202" &&
                    payload["amount"] == 450.00 &&
                    payload["status"] == "pending"
            },
        )
        .times(1)
        .returning(|_, _, _, _| Ok(()));

    producer_mock
        .expect_publish()
        .withf(
            |topic: &str,
             schema: &Option<&str>,
             key: &str,
             payload: &Value| {
                topic == "iam.bronze.events" &&
                    schema.is_none() &&
                    key == "tenant-beta/identity/audit.json" &&
                    payload["user_id"] == "usr_abc123" &&
                    payload["action"] == "login_attempt" &&
                    payload["risk_score"] == 0.12
            },
        )
        .times(1)
        .returning(|_, _, _, _| Ok(()));

    let ingestion_service = IngestionService::new(
        Arc::new(storage_mock),
        Arc::new(producer_mock),
        app_config.pipeline,
    );

    // Multi-row CSV processing.
    let csv_pipeline_execution = ingestion_service
        .process(
            "galadril-bronze".to_string(),
            "tenant-alpha/finance/october_tx.csv".to_string(),
        )
        .await;

    assert!(
        csv_pipeline_execution.is_ok(),
        "The intake pipeline failed while handling structured multi-row CSV payloads: {:?}",
        csv_pipeline_execution.err()
    );

    // Single-document JSON parser route.
    let json_pipeline_execution = ingestion_service
        .process(
            "galadril-bronze".to_string(),
            "tenant-beta/identity/audit.json".to_string(),
        )
        .await;

    assert!(
        json_pipeline_execution.is_ok(),
        "The intake pipeline failed while processing native JSON telemetry frames: {:?}",
        json_pipeline_execution.err()
    );
}
