//! Real S3, libSQL, and gRPC boundaries; the engine fixture only controls
//! timing.
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::{Context, Result, ensure};
use aws_sdk_s3::config::{Credentials, Region};
use document_compiler::adapters::objects::S3Objects;
use document_compiler::adapters::queue::{Queue, QueueHandle};
use document_compiler::application::{Compiler, now};
use document_compiler::grpc::CompilerGrpc;
use document_compiler::ports::{Authorizer, JobQueue, ObjectStore, Sandbox};
use galadril_document_compiler::delegation::{AuthorizationRequest, Grant};
use galadril_document_compiler::domain::{Asset, Rendered, Scope, Template};
use galadril_document_compiler::proto::document_compiler_client::DocumentCompilerClient;
use galadril_document_compiler::proto::document_compiler_server::DocumentCompilerServer;
use galadril_document_compiler::proto::{self};
use testcontainers::core::{IntoContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{GenericImage, ImageExt};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::Request;
use tonic::transport::Server;

struct Authority(AtomicBool);
#[tonic::async_trait]
impl Authorizer for Authority {
    async fn authorize(
        &self,
        capability: &str,
        _: AuthorizationRequest,
    ) -> Result<Grant> {
        ensure!(self.0.load(Ordering::Acquire), "Revoked");
        let tenant = match capability.chars().next() {
            Some('a') => "tenant_a",
            Some('b') => "tenant_b",
            _ => anyhow::bail!("Denied"),
        };
        Ok(Grant {
            tenant: tenant.into(),
            actor: "alice".into(),
            generation: "generation".into(),
            project: None,
            temporary_session: None,
        })
    }
}
struct EngineFixture;
#[tonic::async_trait]
impl Sandbox for EngineFixture {
    async fn compile(&self, project: Rendered) -> Result<Vec<u8>> {
        ensure!(
            project
                .assets
                .iter()
                .any(|asset| asset.path == "letter.cls"),
            "Missing class"
        );
        Ok(b"%PDF-1.7\ntransport fixture\n%%EOF".to_vec())
    }
}
fn authorized<T>(body: T, actor: char) -> Result<Request<T>> {
    let mut request = Request::new(body);
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {}", actor.to_string().repeat(64)).parse()?,
    );
    Ok(request)
}
fn draft() -> Template {
    Template { id: String::new(), version: String::new(), name: "Letter".into(), description: String::new(), scope: Scope::Personal, owner: "alice".into(), project: None, plugin: None, entrypoint: "main.tex".into(), assets: vec![Asset { path: "main.tex".into(), data: br"\documentclass{letter}\begin{document}Hello\end{document}".to_vec() }, Asset { path: "letter.cls".into(), data: br"\LoadClass{article}".to_vec() }], variables: BTreeMap::new() }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn grpc_s3_libsql_preserve_revisions_tenants_idempotence_and_live_revocation()
-> Result<()> {
    let minio = GenericImage::new(
        "bitnamilegacy/minio@sha256",
        "6dabb4a2088c9a79908de3bc05f4586c23ad2182c8908e7e3acbf61c1467fb20",
    )
    .with_exposed_port(9000.tcp())
    .with_wait_for(WaitFor::message_on_stderr("API:"))
    .with_cmd([
        "server",
        "/bitnami/minio/data",
        "--console-address",
        ":9001",
    ])
    .with_env_var("MINIO_ROOT_USER", "minioadmin")
    .with_env_var("MINIO_ROOT_PASSWORD", "minioadmin")
    .start()
    .await?;
    let s3 = aws_sdk_s3::Client::from_conf(
        aws_sdk_s3::config::Builder::new()
            .behavior_version_latest()
            .endpoint_url(format!(
                "http://127.0.0.1:{}",
                minio.get_host_port_ipv4(9000).await?
            ))
            .region(Region::new("us-east-1"))
            .credentials_provider(Credentials::new(
                "minioadmin",
                "minioadmin",
                None,
                None,
                "test",
            ))
            .force_path_style(true)
            .build(),
    );
    s3.create_bucket().bucket("documents").send().await?;
    let objects = Arc::new(S3Objects::new(s3, "documents".into())?);
    let directory = tempfile::tempdir()?;
    let queue: Arc<dyn JobQueue> = Arc::new(QueueHandle::new(Queue::open(
        directory.path().join("queue.db"),
    )?));
    let authority = Arc::new(Authority(AtomicBool::new(true)));
    let compiler = Arc::new(Compiler::new(
        Arc::clone(&queue),
        objects.clone(),
        authority.clone(),
        Arc::new(EngineFixture),
    ));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let (shutdown, stopped) = tokio::sync::oneshot::channel();
    let server_compiler = Arc::clone(&compiler);
    let server = tokio::spawn(async move {
        Server::builder()
            .add_service(DocumentCompilerServer::new(CompilerGrpc(
                server_compiler,
            )))
            .serve_with_incoming_shutdown(
                TcpListenerStream::new(listener),
                async {
                    if stopped.await.is_err() {
                        tracing::debug!("Test shutdown sender dropped");
                    }
                },
            )
            .await
    });
    let mut client =
        DocumentCompilerClient::connect(format!("http://{address}")).await?;
    assert_eq!(
        client
            .list_templates(authorized(
                proto::ListTemplatesRequest::default(),
                'a'
            )?)
            .await?
            .into_inner()
            .templates
            .len(),
        0
    );
    assert_eq!(
        client
            .list_templates(proto::ListTemplatesRequest::default())
            .await
            .err()
            .context("Missing denial")?
            .code(),
        tonic::Code::Unauthenticated
    );
    let reference = client
        .put_template(authorized(
            proto::TemplateArtifact {
                manifest_json: serde_json::to_vec(&draft())?,
            },
            'a',
        )?)
        .await?
        .into_inner();
    let original = client
        .read_template(authorized(reference.clone(), 'a')?)
        .await?
        .into_inner()
        .manifest_json;
    assert!(
        client
            .read_template(authorized(reference.clone(), 'b')?)
            .await
            .is_err()
    );
    assert!(
        client
            .list_templates(authorized(
                proto::ListTemplatesRequest::default(),
                'b'
            )?)
            .await?
            .into_inner()
            .templates
            .is_empty()
    );
    let mut revised: Template = serde_json::from_slice(&original)?;
    revised.description = "Revised metadata".into();
    let revision = client
        .put_template(authorized(
            proto::TemplateArtifact {
                manifest_json: serde_json::to_vec(&revised)?,
            },
            'a',
        )?)
        .await?
        .into_inner();
    assert_ne!(revision.version, reference.version);
    assert_eq!(
        client
            .read_template(authorized(reference.clone(), 'a')?)
            .await?
            .into_inner()
            .manifest_json,
        original
    );
    client
        .publish_template(authorized(reference.clone(), 'a')?)
        .await?;
    let submission = proto::CompileRequest {
        template: Some(reference.clone()),
        document_json: br#"{"variables":{},"source":null}"#.to_vec(),
        idempotency_key: uuid::Uuid::new_v4().to_string(),
    };
    let mut jobs = tokio::task::JoinSet::new();
    for _ in 0..8 {
        let mut client = client.clone();
        let submission = submission.clone();
        jobs.spawn(async move {
            client
                .submit(authorized(submission, 'a')?)
                .await
                .map(|response| response.into_inner())
                .map_err(anyhow::Error::from)
        });
    }
    let mut identifiers = std::collections::BTreeSet::new();
    while let Some(result) = jobs.join_next().await {
        identifiers.insert(result??.job_id);
    }
    assert_eq!(identifiers.len(), 1);
    let job = queue.claim(now()).await?.context("Missing queued work")?;
    assert!(queue.claim(now()).await?.is_none());
    compiler.execute(&job).await?;
    assert!(queue.finish(job.clone(), true).await?);
    // PDF authorization needs durable identifiers, not another source
    // download.
    objects
        .delete(&format!("tenant_a/document-compiler/jobs/{}.json", job.id))
        .await?;
    assert!(
        client
            .read_pdf(authorized(
                proto::JobRef {
                    job_id: job.id.clone()
                },
                'a'
            )?)
            .await?
            .into_inner()
            .content
            .starts_with(b"%PDF-")
    );
    assert!(
        client
            .read_pdf(authorized(
                proto::JobRef {
                    job_id: job.id.clone()
                },
                'b'
            )?)
            .await
            .is_err()
    );
    authority.0.store(false, Ordering::Release);
    assert!(
        client
            .read_template(authorized(reference.clone(), 'a')?)
            .await
            .is_err()
    );
    assert!(
        client
            .read_pdf(authorized(
                proto::JobRef {
                    job_id: job.id.clone()
                },
                'a'
            )?)
            .await
            .is_err()
    );
    assert!(compiler.execute(&job).await.is_err());
    authority.0.store(true, Ordering::Release);
    client
        .delete_template(authorized(reference.clone(), 'a')?)
        .await?;
    assert!(
        client
            .read_template(authorized(reference, 'a')?)
            .await
            .is_err()
    );
    let key = format!("tenant_a/document-compiler/jobs/{}.pdf", job.id);
    objects.delete(&key).await?;
    assert!(!objects.exists(&key).await?);
    shutdown
        .send(())
        .map_err(|_| anyhow::anyhow!("Server stopped early"))?;
    server.await??;
    Ok(())
}
