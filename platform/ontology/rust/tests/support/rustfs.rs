//! The pinned RustFS fixture matches the deployed storage implementation.

use std::error::Error;
use std::time::Duration;

use aws_sdk_s3::Client;
use aws_sdk_s3::config::retry::RetryConfig;
use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
use testcontainers::core::{IntoContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};

pub const BUCKET: &str = "gol-test";
const PORT: u16 = 9000;
const IMAGE: &str = "1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c";
pub type TestError = Box<dyn Error>;

pub struct RustFs {
    pub container: ContainerAsync<GenericImage>,
    pub client: Client,
    pub endpoint: String,
}

pub async fn start() -> Result<RustFs, TestError> {
    let container = GenericImage::new("rustfs/rustfs", IMAGE)
        .with_exposed_port(PORT.tcp())
        .with_wait_for(WaitFor::Nothing)
        .with_mapped_port(0, PORT.tcp())
        .with_host_config_modifier(|config| {
            for binding in config
                .port_bindings
                .iter_mut()
                .flat_map(|bindings| bindings.values_mut())
                .filter_map(Option::as_mut)
                .flatten()
            {
                binding.host_ip = Some("127.0.0.1".into());
            }
        })
        .with_env_var("RUSTFS_ACCESS_KEY", "gol-test-access")
        .with_env_var("RUSTFS_SECRET_KEY", "gol-test-secret")
        .with_env_var("RUSTFS_CONSOLE_ENABLE", "false")
        .with_cmd(["rustfs", "/data"])
        .start()
        .await?;
    let port = container.get_host_port_ipv4(PORT).await?;
    let endpoint = format!("http://127.0.0.1:{port}");
    let health = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(1))
        .build()?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(35);
    loop {
        if health
            .get(format!("{endpoint}/health/ready"))
            .send()
            .await
            .is_ok_and(|response| response.status().is_success())
        {
            break;
        }
        if tokio::time::Instant::now() >= deadline {
            return Err("RustFS storage readiness timed out".into());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let client = client(&endpoint);
    client.create_bucket().bucket(BUCKET).send().await?;
    Ok(RustFs {
        container,
        client,
        endpoint,
    })
}

pub fn client(endpoint: &str) -> Client {
    Client::from_conf(
        aws_sdk_s3::config::Builder::new()
            .behavior_version(BehaviorVersion::latest())
            .region(Region::new("us-east-1"))
            .credentials_provider(Credentials::new(
                "gol-test-access",
                "gol-test-secret",
                None,
                None,
                "test",
            ))
            .retry_config(RetryConfig::disabled())
            .endpoint_url(endpoint)
            .force_path_style(true)
            .build(),
    )
}

pub async fn object_keys(client: &Client) -> Result<Vec<String>, TestError> {
    let objects = client.list_objects_v2().bucket(BUCKET).send().await?;
    Ok(objects
        .contents()
        .iter()
        .filter_map(|object| object.key().map(str::to_owned))
        .collect())
}
