//! Runs the production private GraphQL adapter without unrelated service
//! fixtures.

#[path = "../src/authentication/api.rs"]
mod api;
#[path = "../src/authentication/federation.rs"]
#[expect(
    dead_code,
    reason = "This fixture exercises login synchronization, while configuration uses the full Gateway."
)]
mod federation;
#[path = "../src/authentication/store.rs"]
mod store;

use std::sync::Arc;

use anyhow::{Context, Result};
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
use loth::engine::{EngineSettings, LothEngine};
use loth::{LothConfig, SchemaMode, TextSource};

struct FixtureMembershipWriter(Arc<LothEngine>);

#[async_trait::async_trait]
impl federation::MembershipWriter for FixtureMembershipWriter {
    async fn role_membership(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
        enabled: bool,
    ) -> Result<()> {
        let role = format!("{tenant}/{role}");
        if enabled {
            self.0
                .register_relation("role", &role, "member", "user", user)
                .await?;
        } else {
            self.0
                .revoke_relation("role", &role, "member", "user", user)
                .await?;
        }
        Ok(())
    }

    async fn tenant_membership(&self, tenant: &str, user: &str) -> Result<()> {
        self.0
            .register_relation("tenant", tenant, "member", "user", user)
            .await?;
        Ok(())
    }
}

async fn membership_writer(
    endpoint: &str,
) -> Result<Arc<dyn federation::MembershipWriter>> {
    for attempt in 0..100 {
        let config = LothConfig::new(endpoint, "integration-key")
            .with_zed_schema(TextSource::from_inline(include_str!(
                "../../../schemas/spicedb/schema.zed"
            )));
        let settings = EngineSettings {
            schema_mode: SchemaMode::ApplyIfDifferent,
            enable_replication_fail_closed: false,
        };
        match LothEngine::from_config(config, settings).await {
            Ok((engine, _client)) => {
                return Ok(Arc::new(FixtureMembershipWriter(Arc::new(
                    engine,
                ))));
            },
            Err(error) if attempt == 99 => {
                return Err(error).context("SpiceDB fixture unavailable");
            },
            Err(_) => tracing::info!(
                event.name = "test.spicedb.starting",
                "waiting for the authorization fixture"
            ),
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    anyhow::bail!("SpiceDB fixture did not start")
}

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
        let app = if let Ok(endpoint) = std::env::var("AUTH_SPICEDB_ENDPOINT")
        {
            api::router_with_membership(
                &database,
                key,
                membership_writer(&endpoint).await?,
            )
            .await?
        } else {
            api::build_router(&database, key, None).await?
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:8083").await?;
        axum::serve(listener, app).await?;
        Ok(())
    }
    .await;
    result.and(telemetry.shutdown())
}
