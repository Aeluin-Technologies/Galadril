//! Durable provenance is bound to values; it never replaces live permission
//! checks.

#[path = "../src/application/usecases/gol/receipts.rs"]
mod receipts;

use anyhow::Result;
use galadril_ontology::authorization::{Flow, Operation};
use galadril_ontology::{Classification, ModuleVersion, Version};
use receipts::{Dependency, ReceiptScope, ReceiptSigner};
use serde_json::json;

#[test]
fn expanded_eskg_metadata_fits_the_worker_receipt_transport() -> Result<()> {
    let signer = ReceiptSigner::new(&[17; 32])?;
    let target =
        ModuleVersion::new("e2e.lifecycle".parse()?, Version::new(1, 0, 0))
            .reference("IdentityPrototype".parse()?)
            .into();
    let instance_id = &format!("gol:{}:{}", "a".repeat(64), "b".repeat(64));
    let dependencies = (0..100)
        .map(|index| Dependency {
            ontology: "e2e.lifecycle".into(),
            target: format!(
                "e2e.lifecycle::ObservationState.effect_{index:03}"
            ),
            instance_id: if index < 73 {
                "__schema".into()
            } else {
                instance_id.to_owned()
            },
            revision: "c".repeat(64),
            operation: if index < 73 {
                Operation::Describe
            } else {
                Operation::Read
            },
        })
        .collect();
    let value = json!({"entity_id": instance_id});
    let token = signer.issue(
        &ReceiptScope {
            tenant: "tenant_a",
            target: &target,
            instance_id,
            generation: "abcdef0123456789abcdef0123456789",
            source_revision: "0123456789abcdef0123456789abcdef",
            dependencies: &dependencies,
        },
        &Flow::new(Classification::Restricted),
        &value,
    )?;
    assert!(token.len() <= 65536);
    let verified = signer.verify(&token, "tenant_a", instance_id, &value)?;
    assert!(verified.dependencies == dependencies);
    Ok(())
}

#[test]
fn signed_provenance_cannot_be_rebound_to_another_value_or_resource()
-> Result<()> {
    let signer = ReceiptSigner::new(&[17; 32])?;
    let target = ModuleVersion::new(
        "communication.mail".parse()?,
        Version::new(1, 0, 0),
    )
    .reference("Message".parse()?)
    .into();
    let scope = ReceiptScope {
        tenant: "tenant_a",
        target: &target,
        instance_id: "event_1",
        generation: "abcdef0123456789abcdef0123456789",
        source_revision: "0123456789abcdef0123456789abcdef",
        dependencies: &std::collections::BTreeSet::new(),
    };
    let value = json!({"subject":"confidential","tags":["one","two"]});
    let token = signer.issue(
        &scope,
        &Flow::new(Classification::Restricted),
        &value,
    )?;
    let verified = signer.verify(&token, "tenant_a", "event_1", &value)?;
    assert_eq!(verified.flow.classification, Classification::Restricted);
    assert_eq!(verified.target, target);
    assert!(
        signer
            .verify(&token, "tenant_b", "event_1", &value)
            .is_err()
    );
    assert!(
        signer
            .verify(&token, "tenant_a", "event_2", &value)
            .is_err()
    );
    assert!(
        signer
            .verify(
                &token,
                "tenant_a",
                "event_1",
                &json!({"subject":"public","tags":["one","two"]})
            )
            .is_err()
    );
    assert!(
        signer
            .verify(
                &token,
                "tenant_a",
                "event_1",
                &json!({"subject":"confidential","tags":["two","one"]})
            )
            .is_err()
    );
    assert!(
        ReceiptSigner::new(&[18; 32])?
            .verify(&token, "tenant_a", "event_1", &value)
            .is_err()
    );
    Ok(())
}

#[test]
fn provenance_is_deterministic_and_bounded() -> Result<()> {
    let signer = ReceiptSigner::new(&[17; 32])?;
    let target = ModuleVersion::new(
        "communication.mail".parse()?,
        Version::new(1, 0, 0),
    )
    .reference("Message".parse()?)
    .into();
    let scope = ReceiptScope {
        tenant: "tenant_a",
        target: &target,
        instance_id: "event_1",
        generation: "abcdef0123456789abcdef0123456789",
        source_revision: "0123456789abcdef0123456789abcdef",
        dependencies: &std::collections::BTreeSet::new(),
    };
    let first = signer.issue(
        &scope,
        &Flow::new(Classification::Public),
        &json!({"a":1,"b":2}),
    )?;
    let second = signer.issue(
        &scope,
        &Flow::new(Classification::Public),
        &json!({"b":2,"a":1}),
    )?;
    assert_eq!(first, second);
    assert!(
        signer
            .issue(
                &scope,
                &Flow::new(Classification::Public),
                &json!({"body":"x".repeat(128*1024)})
            )
            .is_err()
    );
    assert!(
        signer
            .verify("not.a.receipt", "tenant_a", "event_1", &json!({}))
            .is_err()
    );
    assert!(ReceiptSigner::new(&[17; 8]).is_err());
    Ok(())
}
