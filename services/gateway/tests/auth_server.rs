//! Runs the production private GraphQL adapter without unrelated service fixtures.

#[path = "../src/authentication/api.rs"]
mod api;
#[path = "../src/authentication/store.rs"]
mod store;

use anyhow::{Context, Result};
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};

#[tokio::main]
async fn main() -> Result<()> {
    let telemetry = TelemetryConfig::Binary {
        name: "galadril-gateway-auth-test",
        version: "test",
    }
    .configure()?;
    let result = async {
        let database = std::env::var("AUTH_DATABASE_URL")
            .context("Missing authentication database")?;
        let key = secrecy::SecretString::from(
            std::env::var("AUTH_GATEWAY_KEY")
                .context("Missing private credential")?,
        );
        let app = api::router(&database, key).await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:8083").await?;
        axum::serve(listener, app).await?;
        Ok(())
    }
    .await;
    result.and(telemetry.shutdown())
}
