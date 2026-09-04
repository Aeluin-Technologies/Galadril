//! Exercises the Rust HTTP adapter against a real TerminusDB server.

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use galadril_versioning::{
    DatabaseScope, TerminusClient, TerminusConfig, named,
};
use reqwest::{Client, RequestBuilder, StatusCode};
use secrecy::SecretString;
use serde_json::{Value, json};
use testcontainers_modules::testcontainers::core::IntoContainerPort;
use testcontainers_modules::testcontainers::runners::AsyncRunner;
use testcontainers_modules::testcontainers::{GenericImage, ImageExt};

const ADMIN_PASSWORD: &str = "root";
const TENANT_PASSWORD: &str = "secret";
const TERMINUS_PORT: u16 = 6363;

async fn send(request: RequestBuilder, operation: &str) -> Result<()> {
    let response = request.send().await?;
    let status = response.status();
    let body = response.text().await?;
    ensure!(
        status.is_success(),
        "TerminusDB {operation} failed with HTTP {status}: {body}"
    );
    Ok(())
}

async fn wait_until_ready(http: &Client, endpoint: &str) -> Result<()> {
    for attempt in 0..60 {
        let response = http
            .get(format!("{endpoint}/api/info"))
            .basic_auth("admin", Some(ADMIN_PASSWORD))
            .send()
            .await;
        if response.is_ok_and(|value| value.status().is_success()) {
            return Ok(());
        }
        if attempt == 59 {
            bail!("TerminusDB did not become ready");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    bail!("TerminusDB readiness loop ended unexpectedly")
}

async fn provision(http: &Client, endpoint: &str) -> Result<TerminusConfig> {
    let admin = |path: &str| {
        http.post(format!("{endpoint}/api/{path}"))
            .basic_auth("admin", Some(ADMIN_PASSWORD))
    };
    send(
        admin("roles").json(&json!({
            "name": "test_writer",
            "action": [
                "branch",
                "instance_read_access",
                "instance_write_access",
                "schema_read_access",
                "commit_read_access",
                "commit_write_access",
                "meta_read_access",
                "meta_write_access"
            ]
        })),
        "role creation",
    )
    .await?;

    let mut tenants = HashMap::new();
    for tenant in ["tenant_a", "tenant_b"] {
        send(
            admin(&format!("db/admin/{tenant}")).json(&json!({
                "label": tenant,
                "schema": false,
                "prefixes": {
                    "@base": "terminusdb:///data/",
                    "@schema": "terminusdb:///schema#"
                }
            })),
            "database creation",
        )
        .await?;
        send(
            admin("users").json(&json!({
                "name": tenant,
                "password": TENANT_PASSWORD
            })),
            "user creation",
        )
        .await?;
        send(
            admin("capabilities").json(&json!({
                "operation": "grant",
                "scope_type": "database",
                "scope": format!("admin/{tenant}"),
                "user": tenant,
                "roles": ["test_writer"]
            })),
            "capability grant",
        )
        .await?;
        tenants.insert(
            tenant.to_owned(),
            DatabaseScope {
                database: tenant.to_owned(),
                user: tenant.to_owned(),
                password: SecretString::from(TENANT_PASSWORD),
            },
        );
    }
    Ok(TerminusConfig {
        endpoint: endpoint.to_owned(),
        organization: "admin".to_owned(),
        tenants,
        bases: None,
    })
}

#[tokio::test]
async fn rust_client_uses_real_native_history_and_tenant_capabilities()
-> Result<()> {
    let container =
        GenericImage::new("terminusdb/terminusdb-server", "v12.0.7")
            .with_exposed_port(TERMINUS_PORT.tcp())
            .with_env_var("TERMINUSDB_ADMIN_PASS", ADMIN_PASSWORD)
            .start()
            .await?;
    let host = container.get_host().await?;
    let port = container.get_host_port_ipv4(TERMINUS_PORT.tcp()).await?;
    let endpoint = format!("http://{host}:{port}");
    let http = Client::builder().timeout(Duration::from_secs(30)).build()?;
    wait_until_ready(&http, &endpoint).await?;
    let config = provision(&http, &endpoint).await?;
    let client = TerminusClient::new(config)?;

    let initial = client.read("tenant_a", "main", false).await?;
    let first = client
        .write(
            "tenant_a",
            &json!({"@id": "pipeline/daily", "name": "daily", "version": 1}),
            &initial.revision,
            "integration-test",
            "Create pipeline",
        )
        .await?;
    let second = client
        .write(
            "tenant_a",
            &json!({"@id": "pipeline/daily", "name": "daily", "version": 2}),
            &first,
            "integration-test",
            "Update pipeline",
        )
        .await?;
    ensure!(first != second, "Native writes reused a commit identifier");

    let current = client.read("tenant_a", "main", false).await?;
    ensure!(current.revision == second, "Branch HEAD was not advanced");
    let current_document = named(&current.documents, "pipeline/daily")
        .context("Current pipeline document is missing")?;
    ensure!(current_document.get("version") == Some(&Value::from(2)));

    let historical = client.read("tenant_a", &first, true).await?;
    let historical_document =
        named(&historical.documents, "pipeline/daily")
            .context("Historical pipeline document is missing")?;
    ensure!(historical_document.get("version") == Some(&Value::from(1)));

    let history = client.history("tenant_a", "pipeline/daily", 10).await?;
    ensure!(
        history.contains(&first),
        "First commit is absent from history"
    );
    ensure!(
        history.contains(&second),
        "Second commit is absent from history"
    );

    let stale = match client
        .write(
            "tenant_a",
            &json!({"@id": "pipeline/daily", "version": 3}),
            &first,
            "integration-test",
            "Stale update",
        )
        .await
    {
        Ok(_) => bail!("A stale native write unexpectedly succeeded"),
        Err(error) => error,
    };
    ensure!(stale.to_string().contains("branch changed"));

    for operation in ["document", "log"] {
        let denied = http
            .get(format!(
                "{endpoint}/api/{operation}/admin/tenant_b/local/branch/main"
            ))
            .basic_auth("tenant_a", Some(TENANT_PASSWORD))
            .query(&[("as_list", "true")])
            .send()
            .await?;
        ensure!(denied.status() == StatusCode::FORBIDDEN);
    }
    Ok(())
}
