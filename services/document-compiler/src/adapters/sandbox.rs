//! Direct Tokio supervision keeps storage credentials outside worker
//! environments.
use std::path::Path;

use anyhow::Result;
use galadril_document_compiler::domain::Rendered;

use crate::ports::Sandbox;

pub struct ProcessSandbox {
    #[cfg(all(target_os = "linux", feature = "supervisor"))]
    supervisor: crate::supervisor::Supervisor,
}

impl ProcessSandbox {
    /// Unsupported platforms fail before serving requests; no fallback engine
    /// exists.
    pub async fn new(
        runtime: impl AsRef<Path>,
        state: impl AsRef<Path>,
    ) -> Result<Self> {
        #[cfg(all(target_os = "linux", feature = "supervisor"))]
        {
            Ok(Self {
                supervisor: crate::supervisor::Supervisor::new(runtime, state)
                    .await?,
            })
        }
        #[cfg(not(all(target_os = "linux", feature = "supervisor")))]
        {
            drop((runtime, state));
            anyhow::bail!("Document compilation requires the Linux supervisor")
        }
    }
}

#[async_trait::async_trait]
impl Sandbox for ProcessSandbox {
    async fn compile(&self, project: Rendered) -> Result<Vec<u8>> {
        #[cfg(all(target_os = "linux", feature = "supervisor"))]
        {
            self.supervisor
                .compile(&serde_json::to_vec(&project)?)
                .await
        }
        #[cfg(not(all(target_os = "linux", feature = "supervisor")))]
        {
            drop(project);
            anyhow::bail!("Document compilation requires the Linux supervisor")
        }
    }
}
