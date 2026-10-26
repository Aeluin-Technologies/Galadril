//! Test-only IPC lets attack tests cancel a future in the production
//! supervisor.
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Result, ensure};
use document_compiler::supervisor::Supervisor;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[tokio::main(worker_threads = 2)]
async fn main() -> Result<()> {
    let supervisor = Arc::new(
        Supervisor::new(
            PathBuf::from(std::env::var("DOCUMENT_COMPILER_WORKER_ROOTFS")?),
            PathBuf::from(std::env::var("DOCUMENT_COMPILER_WORKER_STATE")?),
        )
        .await?,
    );
    let socket =
        PathBuf::from(std::env::var("DOCUMENT_COMPILER_WORKER_SOCKET")?);
    let listener = tokio::net::UnixListener::bind(&socket)?;
    let mut terminate = tokio::signal::unix::signal(
        tokio::signal::unix::SignalKind::terminate(),
    )?;
    let mut tasks = tokio::task::JoinSet::new();
    loop {
        tokio::select! {
            incoming = listener.accept() => {
                let (mut client, _) = incoming?;
                let supervisor = Arc::clone(&supervisor);
                tasks.spawn(async move {
                    let operation = async {
                        let size = usize::try_from(client.read_u32().await?)?;
                        ensure!(size > 0 && size <= 10 * 1024 * 1024, "Invalid frame");
                        let mut bytes = vec![0; size];
                        client.read_exact(&mut bytes).await?;
                        let pdf = tokio::select! {
                            result = supervisor.compile(&bytes) => result?,
                            _ = client.read_u8() => return Ok::<_, anyhow::Error>(()),
                        };
                        client.write_u32(u32::try_from(pdf.len())?).await?;
                        client.write_all(&pdf).await?;
                        Ok(())
                    };
                    if operation.await.is_err() { eprintln!("Test compilation rejected"); }
                });
            },
            _ = terminate.recv() => break,
        }
    }
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    std::fs::remove_file(socket)?;
    Ok(())
}
