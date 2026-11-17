//! Deployment settings may select only the mounts approved for that file kind.

#[path = "../src/deployment_file.rs"]
mod deployment_file;

use std::path::Path;

use deployment_file::DeploymentFile;

#[test]
fn configuration_cannot_select_an_unapproved_file() {
    for path in [
        "/etc/passwd",
        "/etc/gol/../passwd",
        "/run/secrets/gol-effect-key",
    ] {
        assert!(
            DeploymentFile::Authority
                .approved_path(Path::new(path))
                .is_err()
        );
    }
}

#[test]
fn readers_reject_paths_before_opening_them() {
    let forbidden = Path::new("/etc/passwd");
    assert_eq!(
        DeploymentFile::Authority
            .read(forbidden)
            .err()
            .map(|error| error.kind()),
        Some(std::io::ErrorKind::PermissionDenied),
    );
    assert_eq!(
        DeploymentFile::RegistryKey
            .read_key(forbidden)
            .err()
            .map(|error| error.kind()),
        Some(std::io::ErrorKind::PermissionDenied),
    );
}

#[test]
fn approved_mounts_are_bound_to_their_purpose() {
    for (file, path) in [
        (DeploymentFile::Authority, "/etc/gol/gol-authorities.json"),
        (DeploymentFile::Runtime, "/e2e/gol-runtime.json"),
        (
            DeploymentFile::RegistryKey,
            "/run/registry-secrets/gateway-key",
        ),
        (
            DeploymentFile::WorkerKey,
            "/run/gol-worker-secrets/worker-key",
        ),
        (DeploymentFile::EffectKey, "/run/secrets/gol-effect-key"),
    ] {
        assert_eq!(
            file.approved_path(Path::new(path)).ok(),
            Some(Path::new(path))
        );
    }
}
