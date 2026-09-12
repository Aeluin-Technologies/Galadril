//! Defines the Registry semantic boundary before storage is introduced.

use anyhow::{Context, Result, bail};
use galadril_registry::semantics::{
    merge_ontology, semantic_diff, slice_ontology, validate_ontology,
    validate_pipeline,
};
use serde_json::{Value, json};

fn ontology(resources: Value) -> Value {
    json!({"version": "1", "resources": resources})
}

#[test]
fn ontology_rejects_dangling_references_and_inheritance_cycles() -> Result<()>
{
    let invalid = ontology(json!([
        {"resource_id":"object.a","kind":"object_type","display_name":"A","references":[],"attributes":{"extends":["object.b"]}},
        {"resource_id":"object.b","kind":"object_type","display_name":"B","references":["missing"],"attributes":{"extends":["object.a"]}}
    ]));

    let Err(error) = validate_ontology(&invalid) else {
        bail!("invalid graph accepted");
    };
    let message = error.to_string();
    assert!(message.contains("dangling_reference"));
    assert!(message.contains("inheritance_cycle"));
    Ok(())
}

#[test]
fn ontology_rejects_incompatible_inherited_property_types() -> Result<()> {
    let invalid = ontology(json!([
        {"resource_id":"object.base","kind":"object_type","display_name":"Base","references":[],"attributes":{}},
        {"resource_id":"object.child","kind":"object_type","display_name":"Child","references":[],"attributes":{"extends":["object.base"]}},
        {"resource_id":"property.base.name","kind":"property","display_name":"name","owner_id":"object.base","value_type":"string","references":[],"attributes":{}},
        {"resource_id":"property.child.name","kind":"property","display_name":"name","owner_id":"object.child","value_type":"integer","references":[],"attributes":{}}
    ]));

    let Err(error) = validate_ontology(&invalid) else {
        bail!("incompatible type accepted");
    };
    assert!(error.to_string().contains("incompatible_property_type"));
    Ok(())
}

#[test]
fn ontology_reports_owner_inheritance_property_and_link_errors() -> Result<()>
{
    let invalid = ontology(json!([
        {"resource_id":"object.root","kind":"object_type","display_name":"Root","owner_id":"object.child","references":[],"attributes":{}},
        {"resource_id":"object.child","kind":"object_type","display_name":"Child","owner_id":"object.root","references":[],"attributes":{"extends":["missing.parent","property.owner"]}},
        {"resource_id":"property.owner","kind":"property","display_name":"owner","owner_id":"missing.owner","value_type":"missing.type","references":[],"attributes":{}},
        {"resource_id":"property.invalid-owner","kind":"property","display_name":"invalid owner","owner_id":"property.owner","references":[],"attributes":{}},
        {"resource_id":"link.invalid","kind":"link_type","display_name":"Invalid link","references":[],"attributes":{"target_type":"property.owner"}}
    ]));

    let Err(error) = validate_ontology(&invalid) else {
        bail!("invalid ontology relationships accepted");
    };
    let message = error.to_string();
    for issue in [
        "owner_cycle",
        "dangling_owner",
        "invalid_owner_kind",
        "dangling_inheritance",
        "invalid_inheritance_type",
        "missing_property_type",
        "invalid_value_type",
        "dangling_link_source_type",
        "incompatible_link_target_type",
    ] {
        assert!(message.contains(issue), "missing issue: {issue}");
    }
    Ok(())
}

#[test]
fn ontology_accepts_all_resource_kinds_and_string_interfaces() -> Result<()> {
    let valid = ontology(json!([
        {"resource_id":"object.base","kind":"object_type","display_name":"Base","references":[],"attributes":{}},
        {"resource_id":"event.changed","kind":"event_type","display_name":"Changed","references":[],"attributes":{}},
        {"resource_id":"object.child","kind":"object_type","display_name":"Child","references":[],"attributes":{"interfaces":"object.base"}},
        {"resource_id":"property.name","kind":"property","display_name":"Name","owner_id":"object.child","value_type":"string","references":[],"attributes":{}},
        {"resource_id":"link.change","kind":"link_type","display_name":"Change","references":[],"attributes":{"source_type":"object.child","target_type":"event.changed"}},
        {"resource_id":"action.update","kind":"action","display_name":"Update","references":[],"attributes":{}},
        {"resource_id":"function.score","kind":"function","display_name":"Score","references":[],"attributes":{}}
    ]));

    validate_ontology(&valid)
}

#[test]
fn ontology_inheritance_dag_resolves_specialized_object_properties()
-> Result<()> {
    let model = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Person","references":[],"attributes":{}},
        {"resource_id":"property.person.name","kind":"property","display_name":"Name","owner_id":"object.person","value_type":"string","references":[],"attributes":{}},
        {"resource_id":"object.pilot","kind":"object_type","display_name":"Pilot","references":[],"attributes":{"extends":"object.person"}},
        {"resource_id":"property.pilot.flight_history","kind":"property","display_name":"Flight history","owner_id":"object.pilot","value_type":"string","references":[],"attributes":{}}
    ]));

    let sliced =
        slice_ontology(&model, &["object.pilot".to_owned()], &[], true)?;
    let resource_ids = sliced
        .get("resources")
        .and_then(Value::as_array)
        .context("resolved resources missing")?
        .iter()
        .filter_map(|resource| {
            resource.get("resource_id").and_then(Value::as_str)
        })
        .collect::<Vec<_>>();
    assert_eq!(
        resource_ids,
        [
            "object.person",
            "property.person.name",
            "object.pilot",
            "property.pilot.flight_history",
        ]
    );
    Ok(())
}

#[test]
fn ontology_rejects_invalid_shapes_before_graph_construction() {
    for invalid in [
        json!({"resources": []}),
        json!({"version": "", "resources": []}),
        ontology(
            json!([{"resource_id":"bad/id","kind":"object_type","display_name":"Bad","references":[],"attributes":{}}]),
        ),
        ontology(
            json!([{"resource_id":"bad","kind":"unknown","display_name":"Bad","references":[],"attributes":{}}]),
        ),
        ontology(
            json!([{"resource_id":"bad","kind":"object_type","display_name":"Bad","references":42,"attributes":{}}]),
        ),
        ontology(
            json!([{"resource_id":"bad","kind":"object_type","display_name":"Bad","references":[42],"attributes":{}}]),
        ),
    ] {
        assert!(validate_ontology(&invalid).is_err());
    }
}

#[test]
fn pipeline_rejects_cycles_and_missing_ontology_bindings() -> Result<()> {
    let definition = json!({
        "name": "daily",
        "sources": [{"id":"source"}],
        "pipeline": [
            {"step":"a","type":"resolve","input_from":["b"],"params":{"ontology_id":"people"}},
            {"step":"b","type":"sink","input_from":["a"]}
        ]
    });

    let Err(error) = validate_pipeline(&definition, &[]) else {
        bail!("cycle accepted");
    };
    assert!(error.to_string().contains("pipeline_cycle"));
    assert!(error.to_string().contains("missing_ontology_binding"));
    Ok(())
}

#[test]
fn pipeline_reports_duplicate_invalid_and_dangling_nodes() -> Result<()> {
    let definition = json!({
        "sources": [{"id":"source"}, {"id":"source"}],
        "pipeline": [
            {"step":"source","type":"unsupported","input_from":["missing"]},
            {"step":"infer","type":"inference","input_from":["source"]}
        ]
    });

    let Err(error) = validate_pipeline(&definition, &[]) else {
        bail!("invalid pipeline accepted");
    };
    let message = error.to_string();
    for issue in [
        "duplicate_pipeline_node",
        "invalid_step_type",
        "missing_inference_model",
        "dangling_pipeline_dependency",
    ] {
        assert!(message.contains(issue), "missing issue: {issue}");
    }
    Ok(())
}

#[test]
fn semantic_diff_and_three_way_merge_are_resource_aware() -> Result<()> {
    let base = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Person","description":"base","references":[],"attributes":{}}
    ]));
    let left = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Human","description":"base","references":[],"attributes":{}}
    ]));
    let right = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Person","description":"profile","references":[],"attributes":{}}
    ]));

    let changes = semantic_diff(&base, &left)?;
    assert_eq!(changes.len(), 1);
    assert_eq!(
        changes.first().context("semantic change missing")?.path,
        ["display_name"]
    );

    let merged = merge_ontology(&base, &left, &right)?;
    assert!(merged.conflicts.is_empty());
    let value = merged.ontology.context("non-conflicting merge missing")?;
    let resource = value
        .get("resources")
        .and_then(Value::as_array)
        .and_then(|resources| resources.first())
        .context("resource missing")?;
    assert_eq!(resource.get("display_name"), Some(&json!("Human")));
    assert_eq!(resource.get("description"), Some(&json!("profile")));
    Ok(())
}

#[test]
fn three_way_merge_reports_same_field_conflicts() -> Result<()> {
    let base = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Person","references":[],"attributes":{}}
    ]));
    let left = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Human","references":[],"attributes":{}}
    ]));
    let right = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Individual","references":[],"attributes":{}}
    ]));

    let merged = merge_ontology(&base, &left, &right)?;
    assert!(merged.ontology.is_none());
    assert_eq!(merged.conflicts.len(), 1);
    assert_eq!(
        merged
            .conflicts
            .first()
            .context("merge conflict missing")?
            .path,
        ["display_name"]
    );
    Ok(())
}

#[test]
fn semantic_diff_reports_resource_and_field_additions_and_removals()
-> Result<()> {
    let base = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Person","description":"base","references":[],"attributes":{}},
        {"resource_id":"object.removed","kind":"object_type","display_name":"Removed","references":[],"attributes":{}}
    ]));
    let target = ontology(json!([
        {"resource_id":"object.person","kind":"object_type","display_name":"Human","references":[],"attributes":{}},
        {"resource_id":"event.added","kind":"event_type","display_name":"Added","references":[],"attributes":{}}
    ]));

    let changes = semantic_diff(&base, &target)?;
    let operations = changes
        .iter()
        .map(|change| change.operation.as_str())
        .collect::<Vec<_>>();
    assert!(operations.contains(&"add_resource"));
    assert!(operations.contains(&"remove_resource"));
    assert!(operations.contains(&"set_field"));
    assert!(operations.contains(&"remove_field"));
    Ok(())
}

#[test]
fn three_way_merge_handles_independent_resource_addition_and_removal()
-> Result<()> {
    let base = ontology(json!([
        {"resource_id":"object.keep","kind":"object_type","display_name":"Keep","references":[],"attributes":{}},
        {"resource_id":"object.remove","kind":"object_type","display_name":"Remove","references":[],"attributes":{}}
    ]));
    let left = ontology(json!([
        {"resource_id":"object.keep","kind":"object_type","display_name":"Keep","references":[],"attributes":{}},
        {"resource_id":"event.added","kind":"event_type","display_name":"Added","references":[],"attributes":{}}
    ]));
    let right = base.clone();

    let merged = merge_ontology(&base, &left, &right)?
        .ontology
        .context("merged ontology missing")?;
    let resource_ids = merged
        .get("resources")
        .and_then(Value::as_array)
        .context("merged resources missing")?
        .iter()
        .filter_map(|resource| {
            resource.get("resource_id").and_then(Value::as_str)
        })
        .collect::<Vec<_>>();
    assert_eq!(resource_ids, ["event.added", "object.keep"]);
    Ok(())
}
