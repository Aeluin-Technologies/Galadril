//! The isolated worker never initializes telemetry exporters or service
//! clients.
#[cfg(target_os = "linux")]
fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        None => document_compiler::worker::run(),
        Some("--probe") => {
            anyhow::ensure!(
                args.next().is_none(),
                "Unexpected probe arguments"
            );
            document_compiler::worker::probe()
        },
        Some("--install-rootfs") => {
            let target = args
                .next()
                .ok_or_else(|| anyhow::anyhow!("Rootfs directory required"))?;
            anyhow::ensure!(
                args.next().is_none(),
                "Unexpected installer arguments"
            );
            document_compiler::worker::install_rootfs(std::path::Path::new(
                &target,
            ))
        },
        Some(_) => anyhow::bail!("Unsupported worker operation"),
    }
}
#[cfg(not(target_os = "linux"))]
fn main() -> anyhow::Result<()> {
    anyhow::bail!("Document workers require Linux isolation")
}
