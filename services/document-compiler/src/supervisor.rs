//! Trusted API orchestration owns fresh POSIX-spawned workers and watchdogs.
use std::ffi::CString;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream as StdUnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use galadril_document_compiler::domain::{MAX_FRAME, MAX_PDF_BYTES};
use nix::errno::Errno;
use nix::spawn::{
    PosixSpawnAttr, PosixSpawnFileActions, PosixSpawnFlags, posix_spawn,
};
use nix::sys::signal::{Signal, killpg};
use nix::sys::wait::{WaitPidFlag, WaitStatus, waitpid};
use nix::unistd::{Pid, getpid, getuid};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::sandbox_policy::WALL_SECONDS;

struct Running {
    pid: Option<Pid>,
    directory: Option<tempfile::TempDir>,
}
impl Running {
    fn cleanup(&mut self) -> Result<()> {
        if let Some(pid) = self.pid {
            match killpg(pid, Signal::SIGKILL) {
                Ok(()) | Err(Errno::ESRCH) => {},
                Err(error) => return Err(error.into()),
            }
            match waitpid(pid, None) {
                Ok(_) | Err(Errno::ECHILD) => self.pid = None,
                Err(error) => return Err(error.into()),
            }
        }
        if let Some(directory) = self.directory.take() {
            directory.close()?;
        }
        Ok(())
    }

    async fn completed(&mut self) -> Result<()> {
        let pid = self.pid.context("Worker already reaped")?;
        loop {
            match waitpid(pid, Some(WaitPidFlag::WNOHANG)) {
                Ok(WaitStatus::StillAlive) => {
                    tokio::time::sleep(Duration::from_millis(10)).await
                },
                Ok(WaitStatus::Exited(_, code)) => {
                    self.pid = None;
                    ensure!(code == 0, "Worker failed");
                    return Ok(());
                },
                Ok(WaitStatus::Signaled(_, _, _)) => {
                    self.pid = None;
                    anyhow::bail!("Worker terminated");
                },
                Ok(_) => {},
                Err(error) => return Err(error.into()),
            }
        }
    }
}
impl Drop for Running {
    fn drop(&mut self) {
        if self.cleanup().is_err() {
            tracing::error!(
                event.name = "document_compiler.worker.cleanup_failed"
            );
        }
    }
}

/// Only constant arguments and supervisor-owned public paths cross exec.
fn start(
    runtime: &Path,
    state: &Path,
    input: OwnedFd,
    output: OwnedFd,
    probe: bool,
) -> Result<Running> {
    let directory = tempfile::Builder::new()
        .prefix("job-")
        .rand_bytes(16)
        .tempdir_in(state)?;
    std::fs::set_permissions(
        directory.path(),
        std::fs::Permissions::from_mode(0o700),
    )?;
    let executable = runtime.join("compiler-worker");
    let mut actions = PosixSpawnFileActions::init()?;
    actions.add_dup2(input.as_raw_fd(), 0)?;
    actions.add_dup2(output.as_raw_fd(), 1)?;
    let stderr = std::fs::File::open("/dev/null")?;
    actions.add_dup2(stderr.as_raw_fd(), 2)?;
    let mut attributes = PosixSpawnAttr::init()?;
    attributes.set_pgroup(Pid::from_raw(0))?;
    attributes.set_flags(PosixSpawnFlags::POSIX_SPAWN_SETPGROUP)?;
    let mut arguments =
        vec![CString::new(executable.as_os_str().as_encoded_bytes())?];
    if probe {
        arguments.push(CString::new("--probe")?);
    }
    let triple = match std::env::consts::ARCH {
        "x86_64" => "x86_64-linux-gnu",
        "aarch64" => "aarch64-linux-gnu",
        _ => anyhow::bail!("Unsupported worker architecture"),
    };
    let environment = [
        CString::new("PATH=/")?,
        CString::new("LANG=C.UTF-8")?,
        CString::new("TECTONIC_UNTRUSTED_MODE=1")?,
        CString::new("SOURCE_DATE_EPOCH=0")?,
        CString::new(format!("DOCUMENT_COMPILER_PARENT_PID={}", getpid()))?,
        CString::new(format!(
            "DOCUMENT_COMPILER_WORKER_ROOTFS={}",
            runtime.display()
        ))?,
        CString::new(format!(
            "DOCUMENT_COMPILER_WORKSPACE={}",
            directory.path().display()
        ))?,
        CString::new(format!("TMPDIR={}", directory.path().display()))?,
        CString::new(format!("HOME={}", directory.path().display()))?,
        CString::new(format!(
            "LD_LIBRARY_PATH={}/usr/lib/{triple}:{}/lib/{triple}",
            runtime.display(),
            runtime.display()
        ))?,
    ];
    let pid = posix_spawn(
        &executable,
        &actions,
        &attributes,
        &arguments,
        &environment,
    )?;
    Ok(Running {
        pid: Some(pid),
        directory: Some(directory),
    })
}

async fn compile(
    runtime: &Path,
    state: &Path,
    bytes: &[u8],
) -> Result<Vec<u8>> {
    let (parent_in, child_in) = StdUnixStream::pair()?;
    let (parent_out, child_out) = StdUnixStream::pair()?;
    let mut running =
        start(runtime, state, child_in.into(), child_out.into(), false)?;
    parent_in.set_nonblocking(true)?;
    parent_out.set_nonblocking(true)?;
    let mut input = tokio::net::UnixStream::from_std(parent_in)?;
    let output = tokio::net::UnixStream::from_std(parent_out)?;
    let operation = async {
        input.write_all(bytes).await?;
        input.shutdown().await?;
        let mut pdf = Vec::new();
        output
            .take((MAX_PDF_BYTES + 1) as u64)
            .read_to_end(&mut pdf)
            .await?;
        ensure!(
            pdf.starts_with(b"%PDF-") && pdf.len() <= MAX_PDF_BYTES,
            "Worker failed"
        );
        running.completed().await?;
        Ok::<_, anyhow::Error>(pdf)
    };
    let result =
        tokio::time::timeout(Duration::from_secs(WALL_SECONDS), operation)
            .await
            .context("Worker deadline exceeded")
            .and_then(std::convert::identity);
    tokio::task::spawn_blocking(move || running.cleanup()).await??;
    result
}

/// Readiness requires a fresh worker to enforce the actual kernel policy.
async fn probe(runtime: &Path, state: &Path) -> Result<()> {
    let (parent_in, child_in) = StdUnixStream::pair()?;
    let (parent_out, child_out) = StdUnixStream::pair()?;
    let mut child =
        start(runtime, state, child_in.into(), child_out.into(), true)?;
    drop(parent_in);
    parent_out.set_nonblocking(true)?;
    let output = tokio::net::UnixStream::from_std(parent_out)?;
    let result = tokio::time::timeout(Duration::from_secs(5), async {
        let mut bytes = Vec::new();
        output.take(64).read_to_end(&mut bytes).await?;
        child.completed().await?;
        ensure!(bytes == b"ready\n", "Worker policy unavailable");
        Ok::<_, anyhow::Error>(())
    })
    .await;
    tokio::task::spawn_blocking(move || child.cleanup()).await??;
    result??;
    Ok(())
}

/// Shares the API runtime while workers receive only public paths and project
/// bytes.
pub struct Supervisor {
    runtime: PathBuf,
    state: PathBuf,
    permits: tokio::sync::Semaphore,
}

impl Supervisor {
    /// Probes mandatory kernel restrictions before the API can become ready.
    pub async fn new(
        runtime: impl AsRef<Path>,
        state: impl AsRef<Path>,
    ) -> Result<Self> {
        ensure!(
            getuid().as_raw() == 1000,
            "Supervisor must run as the dedicated unprivileged user"
        );
        let runtime = std::fs::canonicalize(runtime)?;
        std::fs::create_dir_all(state.as_ref())?;
        let state = std::fs::canonicalize(state)?;
        ensure!(
            !runtime.starts_with(&state) && !state.starts_with(&runtime),
            "Worker resources must be disjoint"
        );
        std::fs::set_permissions(
            &state,
            std::fs::Permissions::from_mode(0o700),
        )?;
        probe(&runtime, &state).await?;
        tracing::info!(event.name = "document_compiler.sandbox.ready");
        Ok(Self {
            runtime,
            state,
            permits: tokio::sync::Semaphore::new(2),
        })
    }

    /// Dropping this future kills and reaps its worker through the process
    /// guard.
    pub async fn compile(&self, bytes: &[u8]) -> Result<Vec<u8>> {
        ensure!(
            !bytes.is_empty() && bytes.len() <= MAX_FRAME,
            "Worker request exceeds limit"
        );
        let _permit = self
            .permits
            .try_acquire()
            .context("Worker capacity exhausted")?;
        compile(&self.runtime, &self.state, bytes).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unavailable() -> Supervisor {
        Supervisor {
            runtime: PathBuf::from("/missing-runtime"),
            state: PathBuf::from("/missing-state"),
            permits: tokio::sync::Semaphore::new(2),
        }
    }

    #[tokio::test]
    async fn oversized_input_is_rejected_before_starting_a_process()
    -> Result<()> {
        let supervisor = unavailable();
        let error = supervisor
            .compile(&vec![0; MAX_FRAME + 1])
            .await
            .err()
            .context("Oversized request was accepted")?;
        assert!(error.to_string().contains("exceeds limit"));
        assert_eq!(supervisor.permits.available_permits(), 2);
        Ok(())
    }

    #[tokio::test]
    async fn saturated_supervisor_rejects_work_without_spawning() -> Result<()>
    {
        let supervisor = unavailable();
        let _first = supervisor.permits.acquire().await?;
        let _second = supervisor.permits.acquire().await?;
        let error = supervisor
            .compile(b"{}")
            .await
            .err()
            .context("Unbounded work was accepted")?;
        assert!(error.to_string().contains("capacity"));
        Ok(())
    }

    #[tokio::test]
    async fn spawn_failure_releases_capacity_and_removes_workspace()
    -> Result<()> {
        let state = tempfile::tempdir()?;
        let supervisor = Supervisor {
            runtime: PathBuf::from("/missing-runtime"),
            state: state.path().to_path_buf(),
            permits: tokio::sync::Semaphore::new(2),
        };
        assert!(supervisor.compile(b"{}").await.is_err());
        assert_eq!(supervisor.permits.available_permits(), 2);
        assert!(std::fs::read_dir(state.path())?.next().is_none());
        Ok(())
    }
}
