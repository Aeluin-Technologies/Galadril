//! Composition must be immutable, finite, and independent of runtime payloads.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use galadril_registry::composition::{compile, references};
use serde_json::{Value, json};

fn set(value: &mut Value, path: &str, replacement: Value) -> Result<()> {
    *value.pointer_mut(path).context("Fixture path missing")? = replacement;
    Ok(())
}

fn leaf() -> Value {
    json!({"name":"leaf","sources":[{"id":"input"}],"pipeline":[{"step":"sink","type":"sink","input_from":["input"]}]})
}

fn transfer(target: &str) -> Value {
    json!({"name":"root","sources":[{"id":"input"}],"pipeline":[{"step":"send","type":"pipeline","input_from":["input"],"pipeline_ref":{"pipeline_id":target,"revision_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","input_source":"input"}}]})
}

#[test]
fn a_transfer_enters_only_the_selected_source_of_a_reusable_pipeline()
-> Result<()> {
    let child = json!({"name":"multi", "sources":[{"id":"input"},{"id":"external"}], "pipeline":[{"step":"selected","type":"sink","input_from":["input"]},{"step":"independent","type":"sink","input_from":["external"]}]});
    let definitions =
        BTreeMap::from([(("child".to_owned(), "a".repeat(32)), child)]);
    let result =
        compile("root", &"b".repeat(32), &transfer("child"), &definitions)?;
    let steps = result
        .get("pipeline")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow::anyhow!("steps missing"))?;
    ensure!(steps.len() == 2);
    ensure!(
        steps.iter().any(|step| step.pointer("/origin/block_id") ==
            Some(&json!("selected")))
    );
    ensure!(
        !steps.iter().any(|step| step.pointer("/origin/block_id") ==
            Some(&json!("independent")))
    );
    Ok(())
}

#[test]
fn transfer_blocks_cannot_override_native_execution_with_plugin_code() {
    let mut root = transfer("child");
    if let Some(step) = root
        .pointer_mut("/pipeline/0")
        .and_then(Value::as_object_mut)
    {
        step.insert("capability_id".to_owned(), json!("other.algorithm.run"));
    }
    assert!(references(&root).is_err());
}

#[test]
fn route_destinations_must_be_declared_immediate_consumers() -> Result<()> {
    let mut root = leaf();
    set(
        &mut root,
        "/pipeline",
        json!([
            {"step":"choose","type":"route","routing":"regex","input_from":["input"],"branches":{"match":["sink"]}},
            {"step":"middle","type":"sink","input_from":["choose"]},
            {"step":"sink","type":"sink","input_from":["middle"]}
        ]),
    )?;
    assert!(
        compile("root", &"a".repeat(32), &root, &BTreeMap::new()).is_err()
    );
    Ok(())
}

#[test]
fn pinned_transfer_expands_once_and_keeps_ontology_provenance() -> Result<()> {
    let definitions =
        BTreeMap::from([(("child".to_owned(), "a".repeat(32)), leaf())]);
    let result =
        compile("root", &"b".repeat(32), &transfer("child"), &definitions)?;
    let steps = result
        .get("pipeline")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow::anyhow!("missing steps"))?;
    ensure!(steps.len() == 2);
    ensure!(
        steps.first().and_then(|s| s.get("type")) == Some(&json!("forward"))
    );
    let child = steps
        .get(1)
        .ok_or_else(|| anyhow::anyhow!("missing child"))?;
    ensure!(child.get("input_from") == Some(&json!(["send"])));
    ensure!(child.pointer("/origin/pipeline_id") == Some(&json!("child")));
    ensure!(child.pointer("/origin/block_id") == Some(&json!("sink")));
    Ok(())
}

#[test]
fn cycles_are_rejected_even_across_different_revisions() {
    let definitions = BTreeMap::from([
        (("child".to_owned(), "a".repeat(32)), transfer("root")),
        (("root".to_owned(), "a".repeat(32)), leaf()),
    ]);
    assert!(
        compile("root", &"b".repeat(32), &transfer("child"), &definitions)
            .is_err()
    );
}

#[test]
fn repeated_transfer_occurrences_consume_the_three_transfer_budget()
-> Result<()> {
    let mut root = leaf();
    set(&mut root, "/pipeline", Value::Array((0..4).map(|i| json!({"step":format!("send{i}"),"type":"pipeline","input_from":["input"],"pipeline_ref":{"pipeline_id":"child","revision_id":"a".repeat(32),"input_source":"input"}})).collect()))?;
    let definitions =
        BTreeMap::from([(("child".to_owned(), "a".repeat(32)), leaf())]);
    assert!(compile("root", &"b".repeat(32), &root, &definitions).is_err());
    Ok(())
}

#[test]
fn ordinary_local_paths_are_not_subject_to_the_transfer_limit() -> Result<()> {
    let mut root = leaf();
    set(&mut root, "/pipeline", Value::Array((0..4).map(|i| json!({"step":format!("sink{i}"),"type":"sink","input_from":["input"]})).collect()))?;
    compile("root", &"b".repeat(32), &root, &BTreeMap::new())?;
    Ok(())
}

#[test]
fn expanded_blocks_count_each_embedded_occurrence_and_sources() -> Result<()> {
    let mut child = leaf();
    set(&mut child, "/pipeline", Value::Array((0..255).map(|i| json!({"step":format!("sink{i}"),"type":"sink","input_from":["input"]})).collect()))?;
    let definitions =
        BTreeMap::from([(("child".to_owned(), "a".repeat(32)), child)]);
    assert!(
        compile("root", &"b".repeat(32), &transfer("child"), &definitions)
            .is_err()
    );
    Ok(())
}

#[test]
fn missing_revision_and_ambiguous_continuations_fail_closed() {
    assert!(
        compile(
            "root",
            &"b".repeat(32),
            &transfer("child"),
            &BTreeMap::new()
        )
        .is_err()
    );
    let mut root = transfer("child");
    if let Some(steps) = root.get_mut("pipeline").and_then(Value::as_array_mut)
    {
        steps
            .push(json!({"step":"after","type":"sink","input_from":["send"]}));
    }
    let definitions =
        BTreeMap::from([(("child".to_owned(), "a".repeat(32)), leaf())]);
    assert!(compile("root", &"b".repeat(32), &root, &definitions).is_err());
}

#[test]
fn authoring_cannot_forge_compiler_origins_or_cross_tenant_targets()
-> Result<()> {
    let mut forged = leaf();
    forged
        .pointer_mut("/pipeline/0")
        .and_then(Value::as_object_mut)
        .context("Fixture step missing")?
        .insert("origin".to_owned(), json!({"pipeline_id":"secret"}));
    assert!(references(&forged).is_err());
    assert!(references(&transfer("other/secret")).is_err());
    Ok(())
}

#[test]
fn absent_compiler_fields_can_use_canonical_null_defaults() -> Result<()> {
    let mut definition = leaf();
    let step = definition
        .pointer_mut("/pipeline/0")
        .and_then(Value::as_object_mut)
        .context("Fixture step missing")?;
    step.insert("origin".to_owned(), Value::Null);
    step.insert("pipeline_ref".to_owned(), Value::Null);
    compile("root", &"a".repeat(32), &definition, &BTreeMap::new())?;
    Ok(())
}
