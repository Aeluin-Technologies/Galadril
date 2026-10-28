//! One trusted API runtime owns durable admission and isolated worker
//! supervision.
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use document_compiler::adapters::gateway::Gateway;
use document_compiler::adapters::objects::S3Objects;
use document_compiler::adapters::queue::{Queue, QueueHandle};
use document_compiler::adapters::sandbox::ProcessSandbox;
use document_compiler::application::Compiler;
use document_compiler::grpc::CompilerGrpc;
use galadril_document_compiler::domain::MAX_PDF_BYTES;
use galadril_document_compiler::proto::document_compiler_server::DocumentCompilerServer;
use galadril_telemetry::{ConfigureTelemetry, TelemetryConfig};

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["--check-ready"] {
        return check_ready().await;
    }
    if args.as_slice() == ["--prepare-state"] {
        use std::os::unix::fs::PermissionsExt;
        let directory = "/var/lib/document-compiler";
        std::fs::create_dir_all(directory)?;
        // Set permissions before relinquishing ownership; repeated
        // initialization needs only CAP_CHOWN rather than authority to
        // override file ownership.
        if std::fs::metadata(directory)?.permissions().mode() & 0o777 != 0o700
        {
            std::fs::set_permissions(
                directory,
                std::fs::Permissions::from_mode(0o700),
            )?;
        }
        std::os::unix::fs::chown(directory, Some(1000), Some(1000))?;
        return Ok(());
    }
    ensure!(args.is_empty(), "Unsupported compiler arguments");

    let telemetry = TelemetryConfig::Binary {
        name: "galadril-document-compiler",
        version: env!("CARGO_PKG_VERSION"),
    }
    .configure()?;
    let result = serve().await;
    telemetry.shutdown()?;
    result
}

/// Check the existing private health service without initializing application
/// clients.
async fn check_ready() -> Result<()> {
    use tonic::server::NamedService;
    use tonic_health::pb::HealthCheckRequest;
    use tonic_health::pb::health_check_response::ServingStatus;
    use tonic_health::pb::health_client::HealthClient;

    let bind: SocketAddr = std::env::var("DOCUMENT_COMPILER_BIND")
        .unwrap_or_else(|_| "127.0.0.1:50057".into())
        .parse()?;
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        let channel =
            tonic::transport::Endpoint::from_shared(format!("http://{bind}"))?
                .connect()
                .await?;
        let mut client = HealthClient::new(channel);
        let response = client
            .check(HealthCheckRequest {
                service: DocumentCompilerServer::<CompilerGrpc>::NAME.into(),
            })
            .await?
            .into_inner();
        ensure!(
            response.status == ServingStatus::Serving as i32,
            "Compiler is not ready"
        );
        Ok::<(), anyhow::Error>(())
    })
    .await?
}

async fn serve() -> Result<()> {
    let bind: SocketAddr = std::env::var("DOCUMENT_COMPILER_BIND")
        .unwrap_or_else(|_| "127.0.0.1:50057".into())
        .parse()?;
    let mode = std::env::var("DOCUMENT_COMPILER_PROXY_MODE")
        .unwrap_or_else(|_| "sidecar".into());
    ensure!(
        matches!(mode.as_str(), "sidecar" | "ambient") &&
            (bind.ip().is_loopback() || mode == "ambient"),
        "Compiler must be behind the workload proxy"
    );
    let endpoint = std::env::var("DOCUMENT_COMPILER_S3_ENDPOINT")?;
    let access = std::env::var("DOCUMENT_COMPILER_S3_ACCESS_KEY")?;
    let secret = std::env::var("DOCUMENT_COMPILER_S3_SECRET_KEY")?;
    let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .endpoint_url(endpoint)
        .region(aws_config::Region::new(
            std::env::var("DOCUMENT_COMPILER_S3_REGION")
                .unwrap_or_else(|_| "us-east-1".into()),
        ))
        .credentials_provider(aws_sdk_s3::config::Credentials::new(
            access,
            secret,
            None,
            None,
            "document-compiler",
        ))
        .load()
        .await;
    let client = aws_sdk_s3::Client::from_conf(
        aws_sdk_s3::config::Builder::from(&config)
            .force_path_style(true)
            .build(),
    );
    let objects = Arc::new(S3Objects::new(
        client,
        std::env::var("DOCUMENT_COMPILER_S3_BUCKET")?,
    )?);
    objects.probe().await?;
    let sandbox = Arc::new(
        ProcessSandbox::new(
            PathBuf::from(std::env::var("DOCUMENT_COMPILER_WORKER_ROOTFS")?),
            PathBuf::from(
                std::env::var("DOCUMENT_COMPILER_WORKER_STATE")
                    .unwrap_or_else(|_| {
                        "/run/document-compiler-state/jobs".into()
                    }),
            ),
        )
        .await?,
    );
    let queue_path = PathBuf::from(
        std::env::var("DOCUMENT_COMPILER_QUEUE_PATH")
            .unwrap_or_else(|_| "/var/lib/document-compiler/queue.db".into()),
    );
    let queue = Arc::new(QueueHandle::new(
        tokio::task::spawn_blocking(move || Queue::open(queue_path)).await??,
    ));
    let compiler = Arc::new(Compiler::new(
        queue,
        objects,
        Arc::new(Gateway::new(&std::env::var(
            "DOCUMENT_COMPILER_GATEWAY_ENDPOINT",
        )?)?),
        sandbox,
    ));
    let (shutdown, receiver) = tokio::sync::watch::channel(false);
    let mut executor = tokio::spawn(Arc::clone(&compiler).run(receiver));
    let (health, health_service) = tonic_health::server::health_reporter();
    health
        .set_serving::<DocumentCompilerServer<CompilerGrpc>>()
        .await;
    tracing::info!(event.name = "document_compiler.listening", %bind);
    let server = tonic::transport::Server::builder()
        .concurrency_limit_per_connection(32)
        .add_service(health_service)
        .add_service(
            DocumentCompilerServer::new(CompilerGrpc(compiler))
                .max_decoding_message_size(MAX_PDF_BYTES)
                .max_encoding_message_size(MAX_PDF_BYTES + 1024),
        )
        .serve_with_shutdown(bind, async {
            match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
                Ok(mut terminate) => { tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = terminate.recv() => {} } },
                Err(_) => tracing::error!(event.name = "document_compiler.signal.failed"),
            }
        });
    let result = tokio::select! {
        result = server => result,
        result = &mut executor => { result??; anyhow::bail!("Compilation executor stopped unexpectedly"); },
    };
    shutdown.send(true).context("Queue shutdown failed")?;
    executor.await??;
    result.context("Compiler gRPC failed")
}
