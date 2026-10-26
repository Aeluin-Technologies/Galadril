//! A secret-free supervisor owns fresh POSIX-spawned workers and their
//! watchdogs.
use std::ffi::CString;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream as StdUnixStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
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
    mut client: tokio::net::UnixStream,
    runtime: PathBuf,
    state: PathBuf,
) -> Result<()> {
    let size = usize::try_from(
        tokio::time::timeout(Duration::from_secs(5), client.read_u32())
            .await??,
    )?;
    if size == 0 {
        client.write_u32(0).await?;
        return Ok(());
    }
    ensure!(size <= MAX_FRAME, "Worker request exceeds limit");
    let mut bytes = vec![0; size];
    tokio::time::timeout(
        Duration::from_secs(5),
        client.read_exact(&mut bytes),
    )
    .await??;
    let (parent_in, child_in) = StdUnixStream::pair()?;
    let (parent_out, child_out) = StdUnixStream::pair()?;
    let mut running =
        start(&runtime, &state, child_in.into(), child_out.into(), false)?;
    parent_in.set_nonblocking(true)?;
    parent_out.set_nonblocking(true)?;
    let mut input = tokio::net::UnixStream::from_std(parent_in)?;
    let output = tokio::net::UnixStream::from_std(parent_out)?;
    let operation = async {
        input.write_all(&bytes).await?;
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
    let result = tokio::select! {
        result = tokio::time::timeout(Duration::from_secs(WALL_SECONDS), operation) => result.context("Worker deadline exceeded").and_then(std::convert::identity),
        _ = client.read_u8() => Err(anyhow::anyhow!("Compilation client disconnected")),
    };
    tokio::task::spawn_blocking(move || running.cleanup()).await??;
    let pdf = result?;
    client.write_u32(u32::try_from(pdf.len())?).await?;
    client.write_all(&pdf).await?;
    Ok(())
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

pub async fn serve() -> Result<()> {
    ensure!(
        getuid().as_raw() == 1000,
        "Supervisor must run as the dedicated unprivileged user"
    );
    let runtime = std::fs::canonicalize(std::env::var(
        "DOCUMENT_COMPILER_WORKER_ROOTFS",
    )?)?;
    let state = PathBuf::from(
        std::env::var("DOCUMENT_COMPILER_WORKER_STATE")
            .unwrap_or_else(|_| "/run/document-compiler-state/jobs".into()),
    );
    std::fs::create_dir_all(&state)?;
    std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700))?;
    probe(&runtime, &state).await?;
    let mut terminate = tokio::signal::unix::signal(
        tokio::signal::unix::SignalKind::terminate(),
    )?;
    let socket = PathBuf::from(
        std::env::var("DOCUMENT_COMPILER_WORKER_SOCKET")
            .unwrap_or_else(|_| "/run/document-compiler/worker.sock".into()),
    );
    if socket.try_exists()? {
        std::fs::remove_file(&socket)?;
    }
    let listener = tokio::net::UnixListener::bind(&socket)?;
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600))?;
    let permits = Arc::new(tokio::sync::Semaphore::new(2));
    let mut tasks = tokio::task::JoinSet::new();
    loop {
        tokio::select! {
            incoming = listener.accept() => {
                let (client, _) = incoming?;
                if client.peer_cred()?.uid() != 1000 { tracing::warn!(event.name = "document_compiler.supervisor.peer_denied"); continue; }
                let Ok(permit) = Arc::clone(&permits).try_acquire_owned() else { continue; };
                let runtime = runtime.clone(); let state = state.clone();
                tasks.spawn(async move { let _permit = permit; if compile(client, runtime, state).await.is_err() { tracing::warn!(event.name = "document_compiler.worker.failed"); } });
            },
            result = tasks.join_next(), if !tasks.is_empty() => { if let Some(Err(_)) = result { tracing::error!(event.name = "document_compiler.supervisor.task_failed"); } },
            signal = tokio::signal::ctrl_c() => { signal?; break; },
            _ = terminate.recv() => break,
        }
    }
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    std::fs::remove_file(socket)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn readiness_handshake_never_starts_untrusted_work() -> Result<()> {
        let (mut client, server) = tokio::net::UnixStream::pair()?;
        let task = tokio::spawn(compile(
            server,
            PathBuf::from("/missing-runtime"),
            PathBuf::from("/missing-state"),
        ));
        client.write_u32(0).await?;
        assert_eq!(client.read_u32().await?, 0);
        task.await??;
        Ok(())
    }
}
