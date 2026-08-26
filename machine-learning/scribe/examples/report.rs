use anyhow::Result;
use galadril_telemetry::{ConfigureTelemetry as _, TelemetryConfig};
use scribe::ScribeReport;
use scribe::engine::ScribeConfig;
use scribe::tools::database::NoOpProvider;

const PROMPT: &str = "Synthetic report about DSGE models in macroeconomics. The database is not connected, so clearly distinguish unavailable evidence.";

#[tokio::main]
async fn main() -> Result<()> {
    let telemetry = TelemetryConfig::Binary {
        name: "galadril-scribe-report-example",
        version: env!("CARGO_PKG_VERSION"),
    }
    .configure()?;
    let mut config = ScribeConfig::new()?;
    config.max_iterations = 5;
    config.max_seq_len = 4096;
    let engine = ScribeReport::new(config, NoOpProvider).await?;

    tracing::info!(event.name = "scribe.report.started", "generating report");
    let pdf = engine.generate_pdf(PROMPT).await?;
    tokio::fs::write("report.pdf", pdf).await?;
    tracing::info!(
        event.name = "scribe.report.completed",
        path = "report.pdf",
        "report saved"
    );
    tracing::info!(metrics = ?engine.metrics(), "Scribe metrics snapshot");
    telemetry.shutdown()
}
