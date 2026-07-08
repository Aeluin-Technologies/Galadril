//! Integration testing suite for the `intake` processing pipeline.

use std::sync::Arc;

use anyhow::Result;
use async_trait::async_trait;
use intake::application::IngestionService;
use intake::application::router::PipelineRouter;
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
        async fn list_objects(&self, prefix: &str) -> anyhow::Result<Vec<(String, i64)>>;
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

#[tokio::test]
async fn test_intake_pipeline_e2e_lifecycle() {
    let mut storage_mock = MockBlobStorageMock::new();
    let mut producer_mock = MockEventProducerMock::new();

    let transaction_csv_data = b"id,amount,currency,status\ntx_201,99.99,EUR,settled\ntx_202,450.00,USD,pending";
    let identity_json_data = b"{\"user_id\": \"usr_abc123\", \"action\": \"login_attempt\", \"risk_score\": 0.12}";

    // Mock representation of the new PipelineConfig payload structure loaded
    // from the config bucket.
    let pipeline_json_data = json!({
        "sources": [
            {
                "id": "financial-csv-source",
                "topic": "finance.bronze.transactions",
                "schema_path": "schemas/transaction.avsc",
                "match_pattern": "^[^/]+/finance/.*\\.csv$",
                "parser": "csv"
            },
            {
                "id": "identity-json-source",
                "topic": "iam.bronze.events",
                "schema_path": null,
                "match_pattern": "^[^/]+/identity/.*\\.json$",
                "parser": "json"
            }
        ]
    })
    .to_string();

    storage_mock
        .expect_list_objects()
        .withf(|prefix| prefix.ends_with('/'))
        .returning(|prefix| {
            Ok(vec![(format!("{prefix}pipeline.yaml"), 5_000i64)])
        });

    let pipeline_json_bytes = pipeline_json_data.into_bytes();
    storage_mock
        .expect_download_file()
        .withf(|key| key.ends_with("pipeline.yaml"))
        .returning(move |_| Ok(pipeline_json_bytes.clone()));

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

    let storage_mock_arc: Arc<dyn BlobStorage> = Arc::new(storage_mock);
    let producer_mock_arc: Arc<dyn EventProducer> = Arc::new(producer_mock);

    let pipeline_router =
        Arc::new(PipelineRouter::new(Arc::clone(&storage_mock_arc), 10_000));

    let ingestion_service = IngestionService::new(
        Arc::clone(&storage_mock_arc),
        Arc::clone(&producer_mock_arc),
        pipeline_router,
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
