//! Attack fixtures execute the production policy; this binary is never
//! shipped.
use std::io::{Read, Write};

use anyhow::{Context, Result, ensure};
use document_compiler::kernel::restrict_engine;
use document_compiler::sandbox_policy::{MEMORY_BYTES, WorkerPaths};

fn main() -> Result<()> {
    let paths = WorkerPaths::from_environment()?;
    restrict_engine(&paths)?;
    if std::env::args().nth(1).as_deref() == Some("--probe") {
        std::io::stdout().write_all(b"ready\n")?;
        return Ok(());
    }
    let mut input = Vec::new();
    std::io::stdin().take(1024).read_to_end(&mut input)?;
    let project: galadril_document_compiler::domain::Rendered =
        serde_json::from_slice(&input)?;
    match project.entrypoint.as_slice() {
        b"filesystem" => {
            ensure!(
                std::fs::read("/etc/passwd").is_err(),
                "Read escaped runtime"
            );
            let state = paths.workspace.parent().context("Missing parent")?;
            for name in ["private.cls", "queue.db", "storage-secret"] {
                ensure!(
                    std::fs::read(state.join(name)).is_err(),
                    "Read crossed job or service boundary"
                );
            }
            let parent = std::env::var("DOCUMENT_COMPILER_PARENT_PID")?;
            ensure!(
                std::fs::read(format!("/proc/{parent}/environ")).is_err(),
                "Read parent credentials"
            );
            ensure!(
                std::fs::OpenOptions::new()
                    .write(true)
                    .open(&paths.bundle)
                    .is_err(),
                "Runtime became writable"
            );
            std::fs::write(paths.workspace.join("result"), b"own workspace")?;
            ensure!(
                std::fs::read(paths.workspace.join("result"))? ==
                    b"own workspace",
                "Workspace unavailable"
            );
            for key in [
                "AWS_SECRET_ACCESS_KEY",
                "DOCUMENT_COMPILER_S3_ACCESS_KEY",
                "DOCUMENT_COMPILER_S3_SECRET_KEY",
                "DOCUMENT_COMPILER_QUEUE_PATH",
            ] {
                ensure!(
                    std::env::var_os(key).is_none(),
                    "Inherited credential or queue configuration"
                );
            }
            let mut memory = Vec::<u8>::new();
            ensure!(
                memory
                    .try_reserve_exact(usize::try_from(MEMORY_BYTES * 2)?)
                    .is_err(),
                "Address space limit missing"
            );
            std::io::stdout().write_all(b"%PDF-1.7\npolicy fixture\n%%EOF")?;
        },
        b"network" => {
            std::net::TcpStream::connect(std::net::SocketAddrV4::new(
                std::net::Ipv4Addr::LOCALHOST,
                9,
            ))?;
            anyhow::bail!("Network was permitted");
        },
        b"sleep" => {
            std::fs::write(
                paths.workspace.join("pid"),
                nix::unistd::getpid().to_string(),
            )?;
            std::thread::sleep(std::time::Duration::from_secs(120));
            anyhow::bail!("Watchdog did not terminate the worker");
        },
        _ => anyhow::bail!("Unknown attack fixture"),
    }
    Ok(())
}
