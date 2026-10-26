//! Unprivileged process supervision is isolated from API storage credentials.
#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use galadril_telemetry::{ConfigureTelemetry, TelemetryConfig};
        let telemetry = TelemetryConfig::Binary {
            name: "galadril-document-compiler-supervisor",
            version: env!("CARGO_PKG_VERSION"),
        }
        .configure()?;
        let result = document_compiler::supervisor::serve().await;
        telemetry.shutdown()?;
        result
    }
    #[cfg(not(target_os = "linux"))]
    anyhow::bail!("Document supervisor requires Linux isolation")
}
