//! Pipeline structure validation; ontology semantics belong to GOL.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use anyhow::{Context, Result, ensure};
use serde_json::Value;

/// Minimal immutable GOL binding required by pipeline validation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PipelineBinding<'a> {
    /// Block requiring the ontology.
    pub block_id: &'a str,
    /// Tenant-local logical ontology identity.
    pub ontology_id: &'a str,
}

/// Builds and validates a pipeline DAG and its ontology dependencies.
pub fn validate_pipeline(
    definition: &Value,
    bindings: &[PipelineBinding<'_>],
) -> Result<Vec<String>> {
    validate_pipeline_internal(definition, bindings, true)
}

/// Validates pipeline graph structure while bindings are authored separately.
pub fn validate_pipeline_structure(definition: &Value) -> Result<Vec<String>> {
    validate_pipeline_internal(definition, &[], false)
}

fn validate_pipeline_internal(
    definition: &Value,
    bindings: &[PipelineBinding<'_>],
    require_bindings: bool,
) -> Result<Vec<String>> {
    let object = definition
        .as_object()
        .context("pipeline_definition_not_object")?;
    let sources = object
        .get("sources")
        .and_then(Value::as_array)
        .context("pipeline_sources_missing")?;
    let steps = object
        .get("pipeline")
        .and_then(Value::as_array)
        .context("pipeline_steps_missing")?;
    let mut nodes = BTreeSet::new();
    let mut source_ids = BTreeSet::new();
    let mut issues = Vec::new();

    for source in sources {
        let id = required_string(source, "id", "source")?;
        if !nodes.insert(id) {
            issues.push(format!("duplicate_pipeline_node:{id}"));
        }
        source_ids.insert(id);
    }

    let mut step_dependencies: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut required_ontologies: BTreeMap<&str, &str> = BTreeMap::new();
    for step in steps {
        let id = required_string(step, "step", "pipeline step")?;
        if !nodes.insert(id) {
            issues.push(format!("duplicate_pipeline_node:{id}"));
        }
        let kind = required_string(step, "type", "pipeline step")?;
        if !matches!(
            kind,
            "inference" |
                "resolve" |
                "sink" |
                "dbt" |
                "causal" |
                "algorithm" |
                "route" |
                "pipeline" |
                "forward" |
                "filter"
        ) {
            issues.push(format!("invalid_step_type:{id}:{kind}"));
        }
        if kind == "inference" &&
            step.get("model").and_then(Value::as_str).is_none() &&
            step.get("capability_id").and_then(Value::as_str).is_none()
        {
            issues.push(format!("missing_inference_model:{id}"));
        }
        let dependencies = step
            .get("input_from")
            .and_then(Value::as_array)
            .context("pipeline_input_from_missing")?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .context("pipeline dependency must be a string")
            })
            .collect::<Result<Vec<_>>>()?;
        step_dependencies.insert(id, dependencies);
        if let Some(ontology_id) = step
            .get("params")
            .and_then(Value::as_object)
            .and_then(|params| params.get("ontology_id"))
            .and_then(Value::as_str)
        {
            required_ontologies.insert(id, ontology_id);
        }
    }

    for (step, dependencies) in &step_dependencies {
        for dependency in dependencies {
            if !nodes.contains(dependency) {
                issues.push(format!(
                    "dangling_pipeline_dependency:{step}:{dependency}"
                ));
            }
        }
    }
    let bound: BTreeMap<&str, &str> = bindings
        .iter()
        .map(|binding| (binding.block_id, binding.ontology_id))
        .collect();
    if require_bindings {
        for step in steps {
            if step.get("ontology_required").and_then(Value::as_bool) ==
                Some(true)
            {
                let id = required_string(step, "step", "pipeline step")?;
                if !bound.contains_key(id) {
                    issues.push(format!("missing_ontology_binding:{id}"));
                }
            }
        }
    }
    for (block_id, ontology_id) in required_ontologies {
        if require_bindings &&
            bound.get(block_id).copied() != Some(ontology_id)
        {
            issues.push(format!(
                "missing_ontology_binding:{block_id}:{ontology_id}"
            ));
        }
    }

    validate_branch_routes(steps)?;
    let order = topological_order(&source_ids, &step_dependencies);
    if order.is_none() {
        issues.push("pipeline_cycle".to_owned());
    }
    ensure!(issues.is_empty(), "{}", issues.join("; "));
    order.context("pipeline_cycle")
}

fn validate_branch_routes(steps: &[Value]) -> Result<()> {
    for step in steps {
        let id = required_string(step, "step", "pipeline step")?;
        let is_route =
            step.get("type").and_then(Value::as_str) == Some("route");
        let branches = step.get("branches").and_then(Value::as_object);
        if !is_route {
            ensure!(
                branches.is_none_or(|value| value.is_empty()),
                "Only routing blocks declare branches"
            );
            continue;
        }
        ensure!(
            matches!(
                step.get("routing").and_then(Value::as_str),
                Some("regex" | "slm" | "metadata")
            ),
            "Unknown native routing strategy"
        );
        ensure!(
            step.get("capability_id").is_none_or(Value::is_null),
            "Routing cannot select plugin code"
        );
        let branches = branches.context("Routing branches missing")?;
        ensure!(
            !branches.is_empty() && branches.len() <= 32,
            "Invalid routing branches"
        );
        let mut destinations = BTreeSet::new();
        for (label, values) in branches {
            ensure!(
                !label.trim().is_empty() && label.len() <= 128,
                "Invalid branch label"
            );
            let targets =
                values.as_array().context("Invalid branch destinations")?;
            ensure!(!targets.is_empty(), "Empty branch destinations");
            let mut unique = BTreeSet::new();
            for target in targets {
                let target =
                    target.as_str().context("Invalid branch destination")?;
                ensure!(unique.insert(target), "Repeated branch destination");
                destinations.insert(target);
            }
        }
        let children = steps
            .iter()
            .filter(|child| {
                !matches!(
                    child.pointer("/params/trigger").and_then(Value::as_str),
                    Some("cron" | "on_demand")
                ) && child
                    .get("input_from")
                    .and_then(Value::as_array)
                    .is_some_and(|inputs| {
                        inputs.iter().any(|input| input.as_str() == Some(id))
                    })
            })
            .filter_map(|child| child.get("step").and_then(Value::as_str))
            .collect::<BTreeSet<_>>();
        ensure!(
            destinations == children,
            "Branches must cover exactly their immediate consumers"
        );
    }
    Ok(())
}

fn required_string<'a>(
    value: &'a Value,
    field: &str,
    owner: &str,
) -> Result<&'a str> {
    let result = value
        .get(field)
        .and_then(Value::as_str)
        .with_context(|| format!("{owner} {field} missing"))?;
    ensure!(!result.is_empty(), "{owner} {field} empty");
    Ok(result)
}

fn topological_order<'a>(
    sources: &BTreeSet<&'a str>,
    dependencies: &BTreeMap<&'a str, Vec<&'a str>>,
) -> Option<Vec<String>> {
    let mut indegree: BTreeMap<&str, usize> = sources
        .iter()
        .chain(dependencies.keys())
        .map(|node| (*node, 0))
        .collect();
    let mut downstream: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for (step, inputs) in dependencies {
        for input in inputs {
            downstream.entry(input).or_default().push(step);
            if let Some(degree) = indegree.get_mut(step) {
                *degree += 1;
            }
        }
    }
    let mut ready: VecDeque<&str> = indegree
        .iter()
        .filter_map(|(node, degree)| (*degree == 0).then_some(*node))
        .collect();
    let mut order = Vec::with_capacity(indegree.len());
    while let Some(node) = ready.pop_front() {
        if let Some(children) = downstream.get(node) {
            for child in children {
                if let Some(degree) = indegree.get_mut(*child) {
                    *degree = degree.saturating_sub(1);
                    if *degree == 0 {
                        ready.push_back(*child);
                    }
                }
            }
        }
        order.push(node.to_owned());
    }
    (order.len() == indegree.len()).then_some(order)
}
