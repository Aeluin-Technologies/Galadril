//! Pipeline graph checks remain independent of GOL language semantics.

use anyhow::Result;
use galadril_registry::semantics::validate_pipeline;
use serde_json::json;

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
