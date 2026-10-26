//! A process cannot reopen network, escape its filesystem, or spawn
//! descendants.
#[path = "../src/sandbox_policy.rs"]
mod policy;

#[test]
fn architecture_policies_deny_dangerous_syscalls_by_default()
-> anyhow::Result<()> {
    for architecture in ["x86_64", "aarch64"] {
        let policy = policy::seccomp_policy(architecture)?;
        assert_eq!(
            policy.pointer("/engine/mismatch_action"),
            Some(&serde_json::json!("kill_process"))
        );
        let rules = policy
            .pointer("/engine/filter")
            .and_then(serde_json::Value::as_array)
            .ok_or_else(|| anyhow::anyhow!("Missing filter"))?;
        for forbidden in [
            "socket",
            "connect",
            "bind",
            "listen",
            "sendmsg",
            "recvmsg",
            "execve",
            "execveat",
            "clone",
            "clone3",
            "fork",
            "vfork",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "mount",
            "setns",
            "unshare",
            "bpf",
            "seccomp",
            "prctl",
        ] {
            assert!(
                !rules.iter().any(|rule| rule
                    .get("syscall")
                    .and_then(serde_json::Value::as_str) ==
                    Some(forbidden)),
                "{architecture}: {forbidden}"
            );
        }
    }
    assert!(policy::seccomp_policy("unknown").is_err());
    const {
        assert!(
            policy::CPU_SOFT_SECONDS < policy::CPU_HARD_SECONDS &&
                policy::CPU_HARD_SECONDS < policy::WALL_SECONDS
        );
    }
    assert_eq!(policy::MEMORY_BYTES, 1024 * 1024 * 1024);
    assert_eq!(policy::FILE_BYTES, 32 * 1024 * 1024);
    assert_eq!(policy::FILE_DESCRIPTORS, 64);
    Ok(())
}

#[test]
fn resources_are_canonical_disjoint_and_workspaces_are_fresh()
-> anyhow::Result<()> {
    let root = tempfile::tempdir()?;
    let runtime = root.path().join("runtime");
    let workspace = root.path().join("workspace");
    std::fs::create_dir(&runtime)?;
    std::fs::create_dir(&workspace)?;
    assert!(policy::WorkerPaths::new(&runtime, &workspace).is_err());
    std::fs::write(runtime.join("compiler-worker"), b"binary")?;
    std::fs::write(runtime.join("bundle.ttb"), b"bundle")?;
    assert!(policy::WorkerPaths::new(&runtime, &runtime).is_err());
    let paths = policy::WorkerPaths::new(&runtime, &workspace)?;
    assert_eq!(paths.runtime, runtime);
    assert_eq!(paths.workspace, workspace);
    assert_eq!(paths.bundle, runtime.join("bundle.ttb"));
    assert!(paths.formats.is_dir());
    assert!(policy::WorkerPaths::new(&runtime, &workspace).is_err());
    Ok(())
}

#[test]
fn unconfigured_worker_refuses_to_start() {
    assert!(policy::WorkerPaths::from_environment().is_err());
}
