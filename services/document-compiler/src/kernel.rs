//! Mandatory Linux kernel restrictions shared by the engine and attack probes.
use anyhow::{Context, Result, ensure};
/// Kernel restrictions are mandatory and precede any untrusted decoding.
pub fn restrict_engine(
    paths: &crate::sandbox_policy::WorkerPaths,
) -> Result<()> {
    use landlock::{
        ABI, Access, AccessFs, CompatLevel, Compatible, PathBeneath, PathFd,
        Ruleset, RulesetAttr, RulesetCreatedAttr, RulesetStatus,
    };
    use nix::sys::prctl;
    use nix::sys::resource::{Resource, setrlimit};
    use nix::sys::signal::Signal;
    use nix::unistd::{Pid, getppid};

    use crate::sandbox_policy::{
        CPU_HARD_SECONDS, CPU_SOFT_SECONDS, FILE_BYTES, FILE_DESCRIPTORS,
        MEMORY_BYTES,
    };
    ensure!(
        nix::unistd::getuid().as_raw() == 1000,
        "Worker must be unprivileged"
    );
    let parent =
        Pid::from_raw(std::env::var("DOCUMENT_COMPILER_PARENT_PID")?.parse()?);
    ensure!(getppid() == parent, "Supervisor already stopped");
    prctl::set_pdeathsig(Signal::SIGKILL)?;
    ensure!(getppid() == parent, "Supervisor stopped during startup");
    prctl::set_dumpable(false)?;
    for (resource, soft, hard) in [
        (Resource::RLIMIT_CPU, CPU_SOFT_SECONDS, CPU_HARD_SECONDS),
        (Resource::RLIMIT_AS, MEMORY_BYTES, MEMORY_BYTES),
        (Resource::RLIMIT_FSIZE, FILE_BYTES, FILE_BYTES),
        (Resource::RLIMIT_NOFILE, FILE_DESCRIPTORS, FILE_DESCRIPTORS),
        (Resource::RLIMIT_CORE, 0, 0),
        (Resource::RLIMIT_NPROC, 0, 0),
    ] {
        setrlimit(resource, soft, hard)?;
    }
    let descriptors = std::fs::read_dir("/proc/self/fd")?
        .map(|entry| -> Result<i32> {
            Ok(entry?
                .file_name()
                .to_str()
                .context("Invalid descriptor")?
                .parse()?)
        })
        .collect::<Result<Vec<_>>>()?;
    for descriptor in descriptors.into_iter().filter(|fd| *fd > 2) {
        match nix::unistd::close(descriptor) {
            Ok(()) | Err(nix::errno::Errno::EBADF) => {},
            Err(error) => return Err(error.into()),
        }
    }
    let mut threads = std::fs::read_dir("/proc/self/task")?;
    ensure!(
        threads.next().transpose()?.is_some() &&
            threads.next().transpose()?.is_none(),
        "Worker must be single threaded"
    );
    drop(threads);
    std::env::set_current_dir(&paths.workspace)?;
    let all = AccessFs::from_all(ABI::V3);
    let status = Ruleset::default()
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(all)?
        .create()?
        .add_rule(PathBeneath::new(
            PathFd::new(&paths.runtime)?,
            AccessFs::ReadFile | AccessFs::ReadDir,
        ))?
        .add_rule(PathBeneath::new(
            PathFd::new(&paths.workspace)?,
            all & !AccessFs::Execute,
        ))?
        .restrict_self()?;
    ensure!(
        status.ruleset == RulesetStatus::FullyEnforced && status.no_new_privs,
        "Landlock unavailable"
    );
    let policy = serde_json::to_vec(&crate::sandbox_policy::seccomp_policy(
        std::env::consts::ARCH,
    )?)?;
    let filters = seccompiler::compile_from_json(
        policy.as_slice(),
        std::env::consts::ARCH.try_into()?,
    )?;
    seccompiler::apply_filter(
        filters.get("engine").context("Missing engine filter")?,
    )?;
    Ok(())
}
