//! Read GOL deployment inputs from the service's dedicated mounts.

use std::fs::{self, File};
use std::io::{self, Read};
use std::path::Path;

/// Selects a deployment mount independently of the configured path string.
#[derive(Clone, Copy)]
pub enum DeploymentFile {
    /// Approved compiler and runtime authority.
    Authority,
    /// Gateway's immutable runtime profile.
    Runtime,
    /// Private Registry source channel credential.
    RegistryKey,
    /// Private Vision worker channel credential.
    WorkerKey,
    /// Gateway provenance signing credential.
    EffectKey,
}

impl DeploymentFile {
    /// Returns an approved literal path for this input kind.
    pub fn approved_path(
        self,
        configured: &Path,
    ) -> io::Result<&'static Path> {
        let mounts: &[&str] = match self {
            Self::Authority => &[
                "/etc/gol/gol-authorities.json",
                "/etc/galadril/gol-authorities.json",
                "/e2e/gol-authorities.json",
            ],
            Self::Runtime => &[
                "/etc/gol/gol-runtime.json",
                "/etc/galadril/gol-runtime.json",
                "/e2e/gol-runtime.json",
            ],
            Self::RegistryKey => &["/run/registry-secrets/gateway-key"],
            Self::WorkerKey => &["/run/gol-worker-secrets/worker-key"],
            Self::EffectKey => &["/run/secrets/gol-effect-key"],
        };
        mounts
            .iter()
            .find_map(|mount| {
                let approved = Path::new(mount);
                (configured.as_os_str() == approved.as_os_str())
                    .then_some(approved)
            })
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "Unapproved deployment mount",
                )
            })
    }

    /// Reads at most the approved byte budget, including if the file grows.
    pub fn read(self, configured: &Path) -> io::Result<Vec<u8>> {
        let path = self.approved_path(configured)?;
        let limit = match self {
            Self::Authority | Self::Runtime => 1024 * 1024,
            Self::RegistryKey | Self::WorkerKey | Self::EffectKey => 32,
        };
        // Kubernetes rotates projected files using symlinks inside its mounts.
        // Reject special files while allowing those deployment-owned links.
        if !fs::metadata(path)?.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Deployment input is not a regular file",
            ));
        }
        let file = File::open(path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() > limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Deployment input exceeds its file budget",
            ));
        }
        let mut bytes = Vec::new();
        file.take(limit + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Deployment input exceeds its byte budget",
            ));
        }
        Ok(bytes)
    }
}

impl DeploymentFile {
    /// Requires the exact key size regardless of the file's reported length.
    pub fn read_key(self, configured: &Path) -> io::Result<[u8; 32]> {
        if matches!(self, Self::Authority | Self::Runtime) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Deployment input is not a credential",
            ));
        }
        self.read(configured)?.try_into().map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "Deployment credential must contain 32 bytes",
            )
        })
    }
}
