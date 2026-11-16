//! Verify RustFS preconditions before running the publication protocol.

#[path = "support/rustfs.rs"]
mod rustfs;

use galadril_ontology::archive::remote::{
    RemoteError, RemoteOptions, RemoteStore,
};

#[test]
fn rejects_unbounded_or_zero_lifetimes() {
    let options = RemoteOptions {
        reader_window: std::time::Duration::ZERO,
        ..RemoteOptions::default()
    };
    assert!(options.validate().is_err());
}

#[tokio::test]
#[ignore = "starts the version-pinned local RustFS image"]
async fn docker_rustfs_preconditions_reject_versioned_buckets()
-> Result<(), rustfs::TestError> {
    use aws_sdk_s3::types::{BucketVersioningStatus, VersioningConfiguration};

    let fixture = rustfs::start().await?;
    let tenant: galadril_ontology::Name = "test_tenant".parse()?;
    let project: galadril_ontology::QualifiedName =
        "isolated.project".parse()?;
    let connect = || {
        RemoteStore::connect(
            fixture.client.clone(),
            rustfs::BUCKET,
            tenant.clone(),
            project.clone(),
            RemoteOptions::default(),
        )
    };
    let store = connect().await?;
    assert_eq!(store.active().await?, None);
    assert!(rustfs::object_keys(&fixture.client).await?.is_empty());
    let anonymous =
        reqwest::get(format!("{}/{}/", fixture.endpoint, rustfs::BUCKET))
            .await?;
    assert!(anonymous.status().is_client_error());
    fixture
        .client
        .put_bucket_versioning()
        .bucket(rustfs::BUCKET)
        .versioning_configuration(
            VersioningConfiguration::builder()
                .status(BucketVersioningStatus::Enabled)
                .build(),
        )
        .send()
        .await?;
    assert!(matches!(
        connect().await,
        Err(RemoteError::UnsupportedStorage)
    ));
    fixture.container.stop().await?;
    Ok(())
}
