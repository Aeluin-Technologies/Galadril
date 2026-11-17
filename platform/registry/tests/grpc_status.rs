//! Transport failures must not disclose Registry's private storage
//! coordinates.

use anyhow::{Context, Result};
use galadril_ontology::archive::remote::RemoteError;
use tonic::Code;

use super::domain_status;

#[test]
fn remote_archive_conflicts_are_structured_and_storage_failures_are_private() {
    let conflict = domain_status(RemoteError::Conflict.into());
    assert_eq!(conflict.code(), Code::Aborted);
    assert_eq!(conflict.message(), "Registry publication changed");
    let unavailable = domain_status(RemoteError::Unavailable.into());
    assert_eq!(unavailable.code(), Code::Unavailable);
    assert_eq!(unavailable.message(), "Registry storage is unavailable");
    let corrupt = domain_status(RemoteError::Integrity.into());
    assert_eq!(corrupt.code(), Code::DataLoss);
    assert_eq!(
        corrupt.message(),
        "Registry persisted state failed validation"
    );
}

#[test]
fn http_failure_cannot_reveal_repository_paths_or_signed_query_strings()
-> Result<()> {
    let response = tonic::codegen::http::Response::builder()
        .status(503)
        .body(String::new())?;
    let error=reqwest::Response::from(response).error_for_status().err().context("fixture did not produce an HTTP error")?.with_url("http://private-storage.invalid/repositories/secret-repository?signature=sensitive-test-value".parse()?);
    let status = domain_status(anyhow::Error::new(error));
    assert_eq!(status.code(), Code::Unavailable);
    assert_eq!(status.message(), "Registry storage is unavailable");
    assert!(!status.to_string().contains("secret-repository"));
    assert!(!status.to_string().contains("signature"));
    Ok(())
}
