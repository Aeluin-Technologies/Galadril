//! Durable provenance is bound to values; it never replaces live permission
//! checks.

#[path = "../src/application/usecases/gol/receipts.rs"]
mod receipts;

use anyhow::Result;
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use galadril_ontology::authorization::{Flow, Operation};
use galadril_ontology::{Classification, ModuleVersion, Version};
use receipts::{
    CommandHandoff, Dependency, LineageOrigin, ReceiptScope, ReceiptSigner,
};
use ring::hmac;
use serde_json::{Value, json};
use uuid::Uuid;

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
            lineage: None,
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
fn provenance_preserves_repeated_field_dependencies_within_the_wire_budget()
-> Result<()> {
    let signer = ReceiptSigner::new(&[17; 32])?;
    let target =
        ModuleVersion::new("e2e.lifecycle".parse()?, Version::new(1, 0, 0))
            .reference("ObservationState".parse()?)
            .into();
    let dependencies = (0..8)
        .flat_map(|instance| {
            (0..48).map(move |field| Dependency {
                ontology: "e2e.lifecycle".into(),
                target: format!(
                    "e2e.lifecycle::ObservationState.field_{field:02}"
                ),
                instance_id: format!("gol:{instance:064x}:{instance:064x}"),
                revision: format!("{instance:064x}"),
                operation: Operation::Read,
            })
        })
        .collect();
    let value = json!({"kind": "Observation"});
    let token = signer.issue(
        &ReceiptScope {
            tenant: "tenant_a",
            target: &target,
            instance_id: "observation_1",
            generation: "abcdef0123456789abcdef0123456789",
            source_revision: "0123456789abcdef0123456789abcdef",
            dependencies: &dependencies,
            lineage: None,
        },
        &Flow::new(Classification::Restricted),
        &value,
    )?;
    assert!(token.len() <= 65536);
    let verified =
        signer.verify(&token, "tenant_a", "observation_1", &value)?;
    assert!(verified.dependencies == dependencies);
    Ok(())
}

#[test]
fn legacy_receipts_preserve_live_dependencies_for_value_reads() -> Result<()> {
    let key = [17; 32];
    let signer = ReceiptSigner::new(&key)?;
    let target =
        ModuleVersion::new("e2e.lifecycle".parse()?, Version::new(1, 0, 0))
            .reference("ObservationState".parse()?)
            .into();
    let dependencies = [Dependency {
        ontology: String::new(),
        target: String::new(),
        instance_id: "raw:tenant_a/source".into(),
        revision: "original_etag".into(),
        operation: Operation::Read,
    }]
    .into_iter()
    .collect();
    let value = json!({"kind": "Observation"});
    let token = signer.issue(
        &ReceiptScope {
            tenant: "tenant_a",
            target: &target,
            instance_id: "observation_1",
            generation: "abcdef0123456789abcdef0123456789",
            source_revision: "original_etag",
            dependencies: &dependencies,
            lineage: None,
        },
        &Flow::new(Classification::Restricted),
        &value,
    )?;
    let (encoded, _) = token.split_once('.').ok_or_else(|| {
        anyhow::anyhow!("The issued receipt lacks a signature separator")
    })?;
    let mut legacy: Value =
        serde_json::from_slice(&URL_SAFE_NO_PAD.decode(encoded)?)?;
    let fields = legacy.as_object_mut().ok_or_else(|| {
        anyhow::anyhow!("The issued receipt lacks a JSON object")
    })?;
    fields.insert("format".into(), json!(2));
    fields.insert("dependencies".into(), serde_json::to_value(&dependencies)?);
    let bytes = serde_json::to_vec(&legacy)?;
    let tag = hmac::sign(&hmac::Key::new(hmac::HMAC_SHA256, &key), &bytes);
    let legacy_token = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(&bytes),
        URL_SAFE_NO_PAD.encode(tag.as_ref())
    );
    let verified =
        signer.verify(&legacy_token, "tenant_a", "observation_1", &value)?;
    assert!(verified.dependencies == dependencies);
    assert!(verified.lineage.is_none());
    Ok(())
}

#[test]
fn durable_lineage_is_bound_to_the_source_and_exact_publication() -> Result<()>
{
    let signer = ReceiptSigner::new(&[17; 32])?;
    let target =
        ModuleVersion::new("e2e.lifecycle".parse()?, Version::new(1, 0, 0))
            .reference("ObservationState".parse()?)
            .into();
    let origin = LineageOrigin {
        issuer: "gateway".into(),
        subject_id: "original_actor".into(),
        source_object_id: "raw:tenant_a/raw/default/nonce/example".into(),
        source_revision: "original_etag".into(),
        source_marker: [53; 32],
        pipeline_id: "causal_demo".into(),
        pipeline_revision_id: "published_revision".into(),
        pins: Default::default(),
        handoff: Some(CommandHandoff {
            block_id: "causal".into(),
            command_id: Uuid::from_u128(42),
        }),
    };
    let scope = ReceiptScope {
        tenant: "tenant_a",
        target: &target,
        instance_id: "observation_1",
        generation: "abcdef0123456789abcdef0123456789",
        source_revision: "0123456789abcdef0123456789abcdef",
        dependencies: &Default::default(),
        lineage: Some(&origin),
    };
    let token = signer.issue(
        &scope,
        &Flow::new(Classification::Restricted),
        &json!({"kind": "Observation"}),
    )?;
    let lineage = signer.verify_lineage(
        &token,
        "tenant_a",
        &origin.source_object_id,
        "causal_demo",
        "published_revision",
    )?;
    assert_eq!(lineage.lineage, Some(origin.clone()));
    lineage.verify_command("causal", Uuid::from_u128(42))?;
    assert!(
        lineage
            .verify_command("causal", Uuid::from_u128(43))
            .is_err()
    );
    assert!(
        lineage
            .verify_command("other", Uuid::from_u128(42))
            .is_err()
    );
    for (tenant, source, pipeline, revision) in [
        (
            "tenant_b",
            origin.source_object_id.as_str(),
            "causal_demo",
            "published_revision",
        ),
        (
            "tenant_a",
            "raw:tenant_a/other",
            "causal_demo",
            "published_revision",
        ),
        (
            "tenant_a",
            origin.source_object_id.as_str(),
            "other",
            "published_revision",
        ),
        (
            "tenant_a",
            origin.source_object_id.as_str(),
            "causal_demo",
            "other_revision",
        ),
    ] {
        assert!(
            signer
                .verify_lineage(&token, tenant, source, pipeline, revision)
                .is_err()
        );
    }
    let unbound = signer.issue(
        &ReceiptScope {
            lineage: None,
            ..scope
        },
        &Flow::new(Classification::Restricted),
        &json!({"kind": "Observation"}),
    )?;
    assert!(
        signer
            .verify_lineage(
                &unbound,
                "tenant_a",
                &origin.source_object_id,
                "causal_demo",
                "published_revision"
            )
            .is_err()
    );
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
        lineage: None,
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
        lineage: None,
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
