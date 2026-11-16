//! Raw promotion must carry a trusted, version-pinned GOL admission.

use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use super::*;
use crate::application::ports::upload_store::UploadAdmission;
use crate::application::test_support::{
    AuthorizationDecision, TestAuthorization, audit, identity,
};

#[derive(Default)]
struct Storage {
    heads: AtomicUsize,
    copies: Mutex<Vec<(String, String, String)>>,
}

#[async_trait::async_trait]
impl UploadStore for Storage {
    async fn presign_upload(
        &self,
        _key: &str,
        _expires: Duration,
    ) -> Result<String> {
        Ok("https://uploads.invalid".into())
    }

    async fn staging_revision(&self, _key: &str) -> Result<String> {
        self.heads.fetch_add(1, Ordering::SeqCst);
        Ok("\"trusted-etag\"".into())
    }

    async fn finalize_upload(
        &self,
        request: UploadFinalization<'_>,
    ) -> Result<String> {
        let mut copies = self
            .copies
            .lock()
            .map_err(|_| anyhow::anyhow!("test lock"))?;
        anyhow::ensure!(
            !copies
                .iter()
                .any(|(key, _, _)| key == request.destination_key),
            "immutable destination exists"
        );
        copies.push((
            request.destination_key.into(),
            request.staging_revision.into(),
            request.gol_admission.into(),
        ));
        Ok(request.destination_key.into())
    }
}

struct Admission {
    allow: bool,
    calls: AtomicUsize,
}
#[async_trait::async_trait]
impl UploadAdmission for Admission {
    async fn issue_ingress(
        &self,
        tenant: &str,
        user: &str,
        source: &str,
        revision: &str,
        group: &str,
        context: &QueryContext,
    ) -> Result<String> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        anyhow::ensure!(self.allow, "ingress configuration unavailable");
        anyhow::ensure!(
            tenant == "tenant_a" && user == "user_a" && group == "default"
        );
        anyhow::ensure!(
            source.starts_with("tenant_a/raw/default/") &&
                source.ends_with("/file.txt")
        );
        anyhow::ensure!(
            revision == "\"trusted-etag\"" &&
                context.request_id == "trusted-request"
        );
        Ok("trusted-ingress-receipt".into())
    }
}

fn subject_context() -> QueryContext {
    QueryContext {
        request_id: "trusted-request".into(),
        ..QueryContext::default()
    }
}

#[tokio::test]
async fn admission_binds_exact_bytes_and_immutable_name_before_promotion()
-> Result<()> {
    let storage = Arc::new(Storage::default());
    let admission = Arc::new(Admission {
        allow: true,
        calls: AtomicUsize::new(0),
    });
    let (audit, _) = audit();
    let service = UploadService::new(
        storage.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        admission.clone(),
    );
    let nonce = "1".repeat(32);
    let staging = format!("tenant_a/user_a/default/{nonce}");
    let key = service
        .complete_upload(
            "tenant_a",
            "user_a",
            "https://issuer.invalid",
            &subject_context(),
            UploadDestination {
                staging_key: &staging,
                target_name: "file.txt",
                group_id: None,
            },
        )
        .await?;
    assert_eq!(key, format!("tenant_a/raw/default/{nonce}/file.txt"));
    assert_eq!(storage.heads.load(Ordering::SeqCst), 1);
    assert_eq!(admission.calls.load(Ordering::SeqCst), 1);
    let copies = storage
        .copies
        .lock()
        .map_err(|_| anyhow::anyhow!("test lock"))?;
    assert_eq!(
        copies.as_slice(),
        &[(
            key,
            "\"trusted-etag\"".into(),
            "trusted-ingress-receipt".into()
        )]
    );
    Ok(())
}

#[tokio::test]
async fn missing_ingress_authority_has_no_copy_or_relationship_effect()
-> Result<()> {
    let storage = Arc::new(Storage::default());
    let admission = Arc::new(Admission {
        allow: false,
        calls: AtomicUsize::new(0),
    });
    let auth = TestAuthorization::new(AuthorizationDecision::Allow);
    let (audit, _) = audit();
    let service = UploadService::new(
        storage.clone(),
        identity(true),
        auth.clone(),
        audit,
        admission.clone(),
    );
    let staging = format!("tenant_a/user_a/default/{}", "2".repeat(32));
    assert!(
        service
            .complete_upload(
                "tenant_a",
                "user_a",
                "https://issuer.invalid",
                &subject_context(),
                UploadDestination {
                    staging_key: &staging,
                    target_name: "file.txt",
                    group_id: None
                }
            )
            .await
            .is_err()
    );
    assert_eq!(admission.calls.load(Ordering::SeqCst), 1);
    assert!(
        storage
            .copies
            .lock()
            .map_err(|_| anyhow::anyhow!("test lock"))?
            .is_empty()
    );
    assert!(
        auth.mutations
            .lock()
            .map_err(|_| anyhow::anyhow!("test lock"))?
            .is_empty()
    );
    Ok(())
}

#[tokio::test]
async fn invalid_staging_capability_cannot_mint_ingress_or_probe_storage()
-> Result<()> {
    let storage = Arc::new(Storage::default());
    let admission = Arc::new(Admission {
        allow: true,
        calls: AtomicUsize::new(0),
    });
    let (audit, _) = audit();
    let service = UploadService::new(
        storage.clone(),
        identity(true),
        TestAuthorization::new(AuthorizationDecision::Allow),
        audit,
        admission.clone(),
    );
    let staging = format!("tenant_a/other/default/{}", "3".repeat(32));
    assert!(
        service
            .complete_upload(
                "tenant_a",
                "user_a",
                "https://issuer.invalid",
                &subject_context(),
                UploadDestination {
                    staging_key: &staging,
                    target_name: "file.txt",
                    group_id: None
                }
            )
            .await
            .is_err()
    );
    assert_eq!(admission.calls.load(Ordering::SeqCst), 0);
    assert_eq!(storage.heads.load(Ordering::SeqCst), 0);
    assert!(
        storage
            .copies
            .lock()
            .map_err(|_| anyhow::anyhow!("test lock"))?
            .is_empty()
    );
    Ok(())
}
