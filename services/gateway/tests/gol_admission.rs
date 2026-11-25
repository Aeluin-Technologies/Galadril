//! Admissions preserve the provenance of dependent work across requests.

#[path = "../src/application/usecases/gol/admission.rs"]
mod admission;

use std::time::{Duration, Instant};

use admission::{Actor, AdmissionRegistry, VerifiedInput};
use galadril_ontology::Classification;
use galadril_ontology::authorization::{Clearance, Flow};

type TestResult = Result<(), Box<dyn std::error::Error>>;

fn actor() -> Result<Actor, Box<dyn std::error::Error>> {
    Ok(Actor {
        issuer: "trusted_issuer".into(),
        tenant: "tenant_a".into(),
        subject_type: "user".into(),
        subject_id: "alice".into(),
        cedar: "IAM::User::\"alice\"".parse()?,
        clearance: Clearance::new(Classification::Restricted),
    })
}

#[test]
fn admissions_bind_sources_and_never_reset_an_existing_calculation()
-> TestResult {
    let now = Instant::now();
    let registry = AdmissionRegistry::new(8, Duration::from_secs(30))?;
    let token = registry.admit(
        actor()?,
        VerifiedInput {
            source_object_id: "raw:tenant_a/inbox/item".into(),
            revision: "etag_a".into(),
            flow: Flow::new(Classification::Public),
        },
        now,
    )?;
    let admitted = registry
        .resolve(&token, "tenant_a", "raw:tenant_a/inbox/item", now)?
        .ok_or("admission")?;
    admitted
        .context
        .include_input(&Flow::new(Classification::Confidential))?;
    let again = registry
        .resolve(&token, "tenant_a", "raw:tenant_a/inbox/item", now)?
        .ok_or("same admission")?;
    assert_eq!(
        again.context.current_label()?.classification,
        Classification::Confidential
    );
    assert!(
        registry
            .resolve(&token, "tenant_b", "raw:tenant_a/inbox/item", now)?
            .is_none()
    );
    assert!(
        registry
            .resolve(&token, "tenant_a", "raw:tenant_a/inbox/other", now)?
            .is_none()
    );
    assert!(
        registry
            .resolve(
                &token,
                "tenant_a",
                "raw:tenant_a/inbox/item",
                now + Duration::from_secs(30)
            )?
            .is_none()
    );
    Ok(())
}

#[test]
fn separately_verified_inputs_have_isolated_labels_and_bounded_admissions()
-> TestResult {
    let now = Instant::now();
    let registry = AdmissionRegistry::new(2, Duration::from_secs(30))?;
    let secret = registry.admit(
        actor()?,
        VerifiedInput {
            source_object_id: "raw:tenant_a/secret".into(),
            revision: "etag_secret".into(),
            flow: Flow::new(Classification::Restricted),
        },
        now,
    )?;
    let public = registry.admit(
        actor()?,
        VerifiedInput {
            source_object_id: "raw:tenant_a/public".into(),
            revision: "etag_public".into(),
            flow: Flow::new(Classification::Public),
        },
        now,
    )?;
    assert_eq!(
        registry
            .resolve(&secret, "tenant_a", "raw:tenant_a/secret", now)?
            .ok_or("secret")?
            .context
            .current_label()?
            .classification,
        Classification::Restricted
    );
    assert_eq!(
        registry
            .resolve(&public, "tenant_a", "raw:tenant_a/public", now)?
            .ok_or("public")?
            .context
            .current_label()?
            .classification,
        Classification::Public
    );
    assert!(
        registry
            .admit(
                actor()?,
                VerifiedInput {
                    source_object_id: "raw:tenant_a/third".into(),
                    revision: "etag_third".into(),
                    flow: Flow::new(Classification::Public),
                },
                now
            )
            .is_err()
    );
    Ok(())
}

#[test]
fn ingress_provenance_cannot_name_another_tenant() -> TestResult {
    let registry = AdmissionRegistry::new(2, Duration::from_secs(30))?;
    assert!(
        registry
            .admit(
                actor()?,
                VerifiedInput {
                    source_object_id: "raw:tenant_b/public".into(),
                    revision: "revision".into(),
                    flow: Flow::new(Classification::Public),
                },
                Instant::now()
            )
            .is_err()
    );
    Ok(())
}
