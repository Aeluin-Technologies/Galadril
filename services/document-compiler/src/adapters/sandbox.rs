//! Private Unix IPC contains project bytes only, never delegated credentials.
use std::path::PathBuf;

use anyhow::{Result, ensure};
use galadril_document_compiler::domain::{
    MAX_PDF_BYTES, MAX_PROJECT_BYTES, Rendered,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::ports::Sandbox;

pub const MAX_FRAME: usize = MAX_PROJECT_BYTES * 5;
pub struct UnixSandbox {
    socket: PathBuf,
}
impl UnixSandbox {
    pub async fn probe(&self) -> Result<()> {
        let request = async {
            let mut socket =
                tokio::net::UnixStream::connect(&self.socket).await?;
            socket.write_u32(0).await?;
            ensure!(
                socket.read_u32().await? == 0,
                "Invalid supervisor health response"
            );
            Ok(())
        };
        tokio::time::timeout(std::time::Duration::from_secs(3), request)
            .await?
    }

    pub fn new(socket: PathBuf) -> Self {
        Self { socket }
    }
}

#[async_trait::async_trait]
impl Sandbox for UnixSandbox {
    async fn compile(&self, project: Rendered) -> Result<Vec<u8>> {
        let operation = async {
            let bytes = serde_json::to_vec(&project)?;
            ensure!(bytes.len() <= MAX_FRAME, "Worker input exceeds limit");
            let mut socket =
                tokio::net::UnixStream::connect(&self.socket).await?;
            socket.write_u32(u32::try_from(bytes.len())?).await?;
            socket.write_all(&bytes).await?;
            let size = usize::try_from(socket.read_u32().await?)?;
            ensure!(size > 0 && size <= MAX_PDF_BYTES, "Compilation failed");
            let mut pdf = vec![0; size];
            socket.read_exact(&mut pdf).await?;
            ensure!(pdf.starts_with(b"%PDF-"), "Invalid PDF output");
            Ok(pdf)
        };
        tokio::time::timeout(std::time::Duration::from_secs(70), operation)
            .await?
    }
}
