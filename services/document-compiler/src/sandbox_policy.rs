//! Fixed kernel policy is applied before decoding any untrusted project.
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

pub const CPU_SOFT_SECONDS: u64 = 40;
pub const CPU_HARD_SECONDS: u64 = 45;
pub const MEMORY_BYTES: u64 = 1024 * 1024 * 1024;
pub const FILE_BYTES: u64 = 32 * 1024 * 1024;
pub const FILE_DESCRIPTORS: u64 = 64;
pub const WALL_SECONDS: u64 = 60;

const COMMON_SYSCALLS: &[&str] = &[
    "read",
    "write",
    "readv",
    "writev",
    "pread64",
    "pwrite64",
    "close",
    "close_range",
    "lseek",
    "fcntl",
    "ioctl",
    "dup",
    "dup3",
    "openat",
    "faccessat",
    "faccessat2",
    "fstat",
    "newfstatat",
    "statx",
    "statfs",
    "fstatfs",
    "readlinkat",
    "getdents64",
    "getcwd",
    "chdir",
    "mmap",
    "mprotect",
    "munmap",
    "mremap",
    "madvise",
    "brk",
    "msync",
    "fsync",
    "fdatasync",
    "ftruncate",
    "truncate",
    "mkdirat",
    "unlinkat",
    "renameat",
    "renameat2",
    "fchmod",
    "umask",
    "utimensat",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "sigaltstack",
    "set_tid_address",
    "set_robust_list",
    "rseq",
    "futex",
    "getpid",
    "getppid",
    "gettid",
    "getuid",
    "geteuid",
    "getgid",
    "getegid",
    "getgroups",
    "uname",
    "sysinfo",
    "prlimit64",
    "getrandom",
    "clock_gettime",
    "gettimeofday",
    "nanosleep",
    "clock_nanosleep",
    "sched_getaffinity",
    "sched_yield",
    "exit",
    "exit_group",
    "restart_syscall",
];

/// Uses architecture-specific syscall tables without a native C resolver.
pub fn seccomp_policy(architecture: &str) -> Result<Value> {
    let extra: &[&str] = match architecture {
        "x86_64" => &[
            "open",
            "access",
            "stat",
            "lstat",
            "dup2",
            "readlink",
            "mkdir",
            "unlink",
            "rename",
            "chmod",
            "getrlimit",
            "time",
            "arch_prctl",
        ],
        "aarch64" => &[],
        _ => anyhow::bail!("Unsupported worker architecture"),
    };
    let filter: Vec<Value> = COMMON_SYSCALLS
        .iter()
        .chain(extra)
        .map(|name| json!({"syscall": name}))
        .collect();
    Ok(
        json!({"engine":{"mismatch_action":"kill_process","match_action":"allow","filter":filter}}),
    )
}

#[derive(Debug)]
pub struct WorkerPaths {
    pub runtime: PathBuf,
    pub workspace: PathBuf,
    pub bundle: PathBuf,
    pub formats: PathBuf,
}

impl WorkerPaths {
    /// Only the secret-free supervisor supplies paths; model inputs cannot.
    pub fn new(
        runtime: impl AsRef<Path>,
        workspace: impl AsRef<Path>,
    ) -> Result<Self> {
        let runtime = std::fs::canonicalize(runtime)?;
        let workspace = std::fs::canonicalize(workspace)?;
        ensure!(
            runtime.parent().is_some() && workspace.parent().is_some(),
            "Root directories cannot be worker resources"
        );
        ensure!(
            !runtime.starts_with(&workspace) &&
                !workspace.starts_with(&runtime),
            "Worker resources must be disjoint"
        );
        let bundle = runtime.join("bundle.ttb");
        ensure!(
            bundle.is_file() && runtime.join("compiler-worker").is_file(),
            "Offline runtime is incomplete"
        );
        if let Some(entry) = std::fs::read_dir(&workspace)?.next() {
            entry?;
            anyhow::bail!("Worker workspace must be empty");
        }
        let formats = workspace.join("formats");
        std::fs::create_dir(&formats)?;
        Ok(Self {
            runtime,
            workspace,
            bundle,
            formats,
        })
    }

    pub fn from_environment() -> Result<Self> {
        Self::new(
            std::env::var_os("DOCUMENT_COMPILER_WORKER_ROOTFS")
                .context("Worker runtime required")?,
            std::env::var_os("DOCUMENT_COMPILER_WORKSPACE")
                .context("Worker workspace required")?,
        )
    }
}
