//! GOL graph provenance is a strict logical/version/generation tuple.

#[path = "../src/application/ports/relations_store.rs"]
pub mod relations_store;

use anyhow::Result;
use relations_store::{GolReference, GraphNodeKind};
use serde_json::{Value, json};

fn reference() -> Value {
    json!({
        "tenant_id":"tenant_a",
        "ontology_id":"operations.people",
        "ontology_version":"1.2.3",
        "source_revision":"0123456789abcdef0123456789abcdef",
        "generation_id":"abcdef0123456789abcdef0123456789",
        "symbol_key":"operations.people::Person",
        "symbol_kind":"entity"
    })
}

#[test]
fn canonical_gol_reference_roundtrips_without_legacy_keys() -> Result<()> {
    let wire = reference();
    let value: GolReference = serde_json::from_value(wire.clone())?;
    value.validate("tenant_a", GraphNodeKind::Entity)?;
    assert_eq!(serde_json::to_value(value)?, wire);
    Ok(())
}

#[test]
fn legacy_resource_documents_are_rejected_instead_of_reinterpreted() {
    assert!(
        serde_json::from_value::<GolReference>(json!({
            "tenant_id":"tenant_a", "ontology_id":"operations.people",
            "revision_id":"0123456789abcdef0123456789abcdef",
            "resource_id":"object.person", "resource_kind":"object_type"
        }))
        .is_err()
    );
}

#[test]
fn canonical_provenance_is_tenant_scoped() -> Result<()> {
    let value: GolReference = serde_json::from_value(reference())?;
    assert!(value.validate("tenant_b", GraphNodeKind::Entity).is_err());
    Ok(())
}

#[test]
fn malformed_versions_generations_revisions_and_module_keys_fail_closed()
-> Result<()> {
    for (field, invalid) in [
        ("ontology_id", "invalid module"),
        ("ontology_version", "latest"),
        ("ontology_version", ""),
        ("generation_id", "abcdef01-2345-6789-abcd-ef0123456789"),
        ("generation_id", "ABCDEF0123456789ABCDEF0123456789"),
        ("generation_id", "abc"),
        ("source_revision", "short"),
        ("source_revision", "invalid revision with whitespace"),
        ("symbol_key", "other.people::Person"),
        ("symbol_key", "operations.people::Person::Other"),
        ("symbol_key", "operations.people::"),
        ("symbol_key", "object.person"),
        ("symbol_key", "operations.people::Person.name"),
        ("symbol_kind", "object_type"),
        ("symbol_kind", "struct"),
    ] {
        let mut wire = reference();
        wire.as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("fixture object"))?
            .insert(field.into(), invalid.into());
        let value: GolReference = serde_json::from_value(wire)?;
        assert!(
            value.validate("tenant_a", GraphNodeKind::Entity).is_err(),
            "accepted invalid {field}"
        );
    }
    Ok(())
}

#[test]
fn structural_entities_and_events_require_gol_entities() -> Result<()> {
    let value: GolReference = serde_json::from_value(reference())?;
    for kind in [
        GraphNodeKind::Entity,
        GraphNodeKind::Event,
        GraphNodeKind::LatentIdentity,
    ] {
        value.validate("tenant_a", kind)?;
    }
    for kind in [
        GraphNodeKind::State,
        GraphNodeKind::CausalVariable,
        GraphNodeKind::Inference,
        GraphNodeKind::Decision,
        GraphNodeKind::Metric,
    ] {
        assert!(value.validate("tenant_a", kind).is_err());
    }
    Ok(())
}

#[test]
fn property_nodes_require_an_explicit_canonical_field() -> Result<()> {
    let mut wire = reference();
    let fields = wire
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("fixture object"))?;
    fields
        .insert("symbol_key".into(), "operations.people::Person.name".into());
    fields.insert("symbol_kind".into(), "field".into());
    let value: GolReference = serde_json::from_value(wire.clone())?;
    value.validate("tenant_a", GraphNodeKind::State)?;
    value.validate("tenant_a", GraphNodeKind::CausalVariable)?;
    assert!(value.validate("tenant_a", GraphNodeKind::Entity).is_err());
    for key in [
        "operations.people::Person",
        "operations.people::Person.name.extra",
        "operations.people::Person..name",
        "operations.people::Person.invalid-name",
    ] {
        wire.as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("fixture object"))?
            .insert("symbol_key".into(), key.into());
        let value: GolReference = serde_json::from_value(wire.clone())?;
        assert!(value.validate("tenant_a", GraphNodeKind::State).is_err());
    }
    Ok(())
}

#[test]
fn unknown_reference_fields_are_rejected() -> Result<()> {
    let mut wire = reference();
    wire.as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("fixture object"))?
        .insert("binary_offset".into(), 7.into());
    assert!(serde_json::from_value::<GolReference>(wire).is_err());
    Ok(())
}
