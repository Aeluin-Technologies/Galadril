//! Verifies the executor kernel independently of the nested Docker daemon.
#![cfg(target_os = "linux")]
use landlock::{
    ABI, Access, AccessFs, CompatLevel, Compatible, Ruleset, RulesetAttr,
};

#[test]
fn executor_provides_required_landlock_rights() -> anyhow::Result<()> {
    let ruleset = Ruleset::default();
    let kernel_error = std::io::Error::last_os_error();
    eprintln!("Executor Landlock: {ruleset:?}, {kernel_error}");
    eprintln!(
        "Executor kernel: {}",
        std::fs::read_to_string("/proc/sys/kernel/osrelease")?
    );
    ruleset
        .set_compatibility(CompatLevel::HardRequirement)
        .handle_access(AccessFs::from_all(ABI::V3))?
        .create()?;
    Ok(())
}
