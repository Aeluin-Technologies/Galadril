//! Canonical ontology graph, pipeline DAG, diff, and merge implementation.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value};

const SCALAR_TYPES: &[&str] = &[
    "boolean", "bytes", "date", "datetime", "decimal", "float", "geopoint",
    "integer", "json", "string", "uuid",
];
const RESOURCE_KINDS: &[&str] = &[
    "object_type",
    "event_type",
    "property",
    "link_type",
    "action",
    "function",
];

#[derive(Debug, Clone, PartialEq)]
/// One deterministic field- or resource-level ontology change.
pub struct SemanticChange {
    /// Stable semantic identity of the changed resource.
    pub resource_id: String,
    /// Domain operation independent of the storage representation.
    pub operation: String,
    /// Field path within the resource; empty for whole-resource changes.
    pub path: Vec<String>,
    /// Canonical value before the change, when present.
    pub before: Option<Value>,
    /// Canonical value after the change, when present.
    pub after: Option<Value>,
}

#[derive(Debug, Clone, PartialEq)]
/// One field whose independent branch edits cannot be reconciled safely.
pub struct MergeConflict {
    /// Stable semantic identity of the conflicting resource.
    pub resource_id: String,
    /// Field path within the resource; empty for whole-resource conflicts.
    pub path: Vec<String>,
    /// Common ancestor value, when the field existed in the ancestor.
    pub base: Option<Value>,
    /// Left branch value, when the field exists on the left.
    pub left: Option<Value>,
    /// Right branch value, when the field exists on the right.
    pub right: Option<Value>,
}

#[derive(Debug, Clone, PartialEq)]
/// Semantic three-way merge outcome with either an ontology or conflicts.
pub struct MergeResult {
    /// Valid merged ontology; absent whenever conflicts exist.
    pub ontology: Option<Value>,
    /// Deterministically ordered semantic conflicts.
    pub conflicts: Vec<MergeConflict>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
/// Minimal pipeline-to-ontology dependency used during DAG validation.
pub struct PipelineBinding<'a> {
    /// Pipeline step receiving the ontology slice.
    pub block_id: &'a str,
    /// Tenant-local ontology identity resolved for the step.
    pub ontology_id: &'a str,
}

struct Resource<'a> {
    id: &'a str,
    kind: &'a str,
    display_name: &'a str,
    owner: Option<&'a str>,
    value_type: Option<&'a str>,
    references: Vec<&'a str>,
    parents: Vec<&'a str>,
    attributes: &'a Map<String, Value>,
}

/// Builds and validates the complete ontology graph.
pub fn validate_ontology(ontology: &Value) -> Result<()> {
    let resources = parse_resources(ontology)?;
    let by_id: BTreeMap<&str, &Resource<'_>> = resources
        .iter()
        .map(|resource| (resource.id, resource))
        .collect();
    let mut issues = Vec::new();

    for resource in &resources {
        if let Some(owner) = resource.owner {
            match by_id.get(owner) {
                None => issues
                    .push(format!("dangling_owner:{}:{}", resource.id, owner)),
                Some(owner_resource)
                    if !matches!(
                        owner_resource.kind,
                        "object_type" | "event_type"
                    ) =>
                {
                    issues.push(format!(
                        "invalid_owner_kind:{}:{}",
                        resource.id, owner
                    ));
                },
                Some(_) => {},
            }
        }
        for reference in &resource.references {
            if !by_id.contains_key(reference) {
                issues.push(format!(
                    "dangling_reference:{}:{}",
                    resource.id, reference
                ));
            }
        }
        for parent in &resource.parents {
            match by_id.get(parent) {
                None => issues.push(format!(
                    "dangling_inheritance:{}:{}",
                    resource.id, parent
                )),
                Some(parent_resource)
                    if !matches!(
                        parent_resource.kind,
                        "object_type" | "event_type"
                    ) =>
                {
                    issues.push(format!(
                        "invalid_inheritance_type:{}:{}",
                        resource.id, parent
                    ));
                },
                Some(_) => {},
            }
        }
        if resource.kind == "property" {
            match resource.value_type {
                None | Some("") => issues
                    .push(format!("missing_property_type:{}", resource.id)),
                Some(value_type)
                    if !SCALAR_TYPES.contains(&value_type) &&
                        !by_id.contains_key(value_type) =>
                {
                    issues.push(format!(
                        "invalid_value_type:{}:{}",
                        resource.id, value_type
                    ));
                },
                Some(_) => {},
            }
        }
        if resource.kind == "link_type" {
            validate_link_endpoint(
                resource,
                "source_type",
                &by_id,
                &mut issues,
            );
            validate_link_endpoint(
                resource,
                "target_type",
                &by_id,
                &mut issues,
            );
        }
    }

    detect_cycles(&resources, &by_id, &mut issues);
    validate_inherited_properties(&resources, &by_id, &mut issues);
    ensure!(issues.is_empty(), "{}", issues.join("; "));
    Ok(())
}

/// Validates a non-empty resource selector against one ontology DAG.
pub fn validate_ontology_selector(
    ontology: &Value,
    resource_ids: &[String],
    resource_kinds: &[String],
) -> Result<()> {
    validate_ontology(ontology)?;
    validate_selector(ontology, resource_ids, resource_kinds)
}

/// Resolves a dependency-closed subgraph from one immutable ontology.
pub fn slice_ontology(
    ontology: &Value,
    resource_ids: &[String],
    resource_kinds: &[String],
    include_dependencies: bool,
) -> Result<Value> {
    validate_ontology(ontology)?;
    validate_selector(ontology, resource_ids, resource_kinds)?;
    let resources = ontology
        .get("resources")
        .and_then(Value::as_array)
        .context("ontology resources missing")?;
    let by_id: BTreeMap<&str, &Value> = resources
        .iter()
        .filter_map(|resource| {
            resource
                .get("resource_id")
                .and_then(Value::as_str)
                .map(|id| (id, resource))
        })
        .collect();
    let mut properties_by_owner: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for resource in resources {
        if resource.get("kind").and_then(Value::as_str) == Some("property") &&
            let Some(owner) =
                resource.get("owner_id").and_then(Value::as_str) &&
            let Some(id) =
                resource.get("resource_id").and_then(Value::as_str)
        {
            properties_by_owner.entry(owner).or_default().push(id);
        }
    }
    let mut selected: BTreeSet<&str> =
        resources
            .iter()
            .filter(|resource| {
                resource
                    .get("resource_id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| {
                        resource_ids.iter().any(|item| item == id)
                    }) ||
                    resource
                        .get("kind")
                        .and_then(Value::as_str)
                        .is_some_and(|kind| {
                            resource_kinds.iter().any(|item| item == kind)
                        })
            })
            .filter_map(|resource| {
                resource.get("resource_id").and_then(Value::as_str)
            })
            .collect();
    if include_dependencies {
        let mut pending: Vec<&str> = selected.iter().copied().collect();
        while let Some(id) = pending.pop() {
            let Some(resource) = by_id.get(id) else {
                continue;
            };
            let mut dependencies =
                string_array(resource.get("references"), "references")?;
            if let Some(owner) =
                resource.get("owner_id").and_then(Value::as_str)
            {
                dependencies.push(owner);
            }
            if let Some(attributes) =
                resource.get("attributes").and_then(Value::as_object)
            {
                for field in
                    ["extends", "interfaces", "source_type", "target_type"]
                {
                    dependencies
                        .extend(string_array(attributes.get(field), field)?);
                }
            }
            if let Some(properties) = properties_by_owner.get(id) {
                dependencies.extend(properties);
            }
            for dependency in dependencies {
                if by_id.contains_key(dependency) &&
                    selected.insert(dependency)
                {
                    pending.push(dependency);
                }
            }
        }
    }
    let sliced = resources
        .iter()
        .filter(|resource| {
            resource
                .get("resource_id")
                .and_then(Value::as_str)
                .is_some_and(|id| selected.contains(id))
        })
        .cloned()
        .collect::<Vec<_>>();
    let result = serde_json::json!({
        "version": ontology.get("version").cloned().context("ontology version missing")?,
        "resources": sliced,
    });
    validate_ontology(&result)?;
    Ok(result)
}

fn validate_selector(
    ontology: &Value,
    resource_ids: &[String],
    resource_kinds: &[String],
) -> Result<()> {
    let resources = ontology
        .get("resources")
        .and_then(Value::as_array)
        .context("ontology resources missing")?;
    let ids = resources
        .iter()
        .filter_map(|resource| {
            resource.get("resource_id").and_then(Value::as_str)
        })
        .collect::<BTreeSet<_>>();
    for id in resource_ids {
        ensure!(
            ids.contains(id.as_str()),
            "selector references missing resource"
        );
    }
    for kind in resource_kinds {
        ensure!(
            RESOURCE_KINDS.contains(&kind.as_str()),
            "selector has invalid resource kind"
        );
    }
    ensure!(
        !resource_ids.is_empty() || !resource_kinds.is_empty(),
        "ontology selector is empty"
    );
    Ok(())
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
        if !matches!(kind, "inference" | "resolve" | "sink" | "dbt" | "causal")
        {
            issues.push(format!("invalid_step_type:{id}:{kind}"));
        }
        if kind == "inference" &&
            step.get("model").and_then(Value::as_str).is_none()
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
    for (block_id, ontology_id) in required_ontologies {
        if require_bindings &&
            bound.get(block_id).copied() != Some(ontology_id)
        {
            issues.push(format!(
                "missing_ontology_binding:{block_id}:{ontology_id}"
            ));
        }
    }

    let order = topological_order(&source_ids, &step_dependencies);
    if order.is_none() {
        issues.push("pipeline_cycle".to_owned());
    }
    ensure!(issues.is_empty(), "{}", issues.join("; "));
    order.context("pipeline_cycle")
}

/// Computes deterministic field-level changes keyed by semantic resource ID.
pub fn semantic_diff(
    base: &Value,
    target: &Value,
) -> Result<Vec<SemanticChange>> {
    let base_resources = resource_values(base)?;
    let target_resources = resource_values(target)?;
    let ids: BTreeSet<&str> = base_resources
        .keys()
        .chain(target_resources.keys())
        .copied()
        .collect();
    let mut changes = Vec::new();
    for id in ids {
        match (base_resources.get(id), target_resources.get(id)) {
            (None, Some(after)) => changes.push(SemanticChange {
                resource_id: id.to_owned(),
                operation: "add_resource".to_owned(),
                path: Vec::new(),
                before: None,
                after: Some((*after).clone()),
            }),
            (Some(before), None) => changes.push(SemanticChange {
                resource_id: id.to_owned(),
                operation: "remove_resource".to_owned(),
                path: Vec::new(),
                before: Some((*before).clone()),
                after: None,
            }),
            (Some(before), Some(after)) => {
                diff_value(id, &[], before, after, &mut changes);
            },
            (None, None) => {},
        }
    }
    Ok(changes)
}

/// Performs a deterministic semantic three-way merge without lakeFS merge.
pub fn merge_ontology(
    base: &Value,
    left: &Value,
    right: &Value,
) -> Result<MergeResult> {
    validate_ontology(base)?;
    validate_ontology(left)?;
    validate_ontology(right)?;
    let base_resources = resource_values(base)?;
    let left_resources = resource_values(left)?;
    let right_resources = resource_values(right)?;
    let ids: BTreeSet<&str> = base_resources
        .keys()
        .chain(left_resources.keys())
        .chain(right_resources.keys())
        .copied()
        .collect();
    let mut merged_resources = Vec::new();
    let mut conflicts = Vec::new();

    for id in ids {
        let merged = merge_value(
            id,
            &[],
            base_resources.get(id).copied(),
            left_resources.get(id).copied(),
            right_resources.get(id).copied(),
            &mut conflicts,
        );
        if let Some(value) = merged {
            merged_resources.push(value);
        }
    }
    if !conflicts.is_empty() {
        return Ok(MergeResult {
            ontology: None,
            conflicts,
        });
    }
    let version = left
        .get("version")
        .cloned()
        .or_else(|| right.get("version").cloned())
        .context("ontology version missing")?;
    let ontology = serde_json::json!({
        "version": version,
        "resources": merged_resources,
    });
    validate_ontology(&ontology)?;
    Ok(MergeResult {
        ontology: Some(ontology),
        conflicts,
    })
}

fn parse_resources(ontology: &Value) -> Result<Vec<Resource<'_>>> {
    let version = ontology
        .get("version")
        .and_then(Value::as_str)
        .context("ontology_version_missing")?;
    ensure!(!version.is_empty(), "ontology_version_empty");
    let values = ontology
        .get("resources")
        .and_then(Value::as_array)
        .context("ontology_resources_missing")?;
    let mut ids = BTreeSet::new();
    let mut resources = Vec::with_capacity(values.len());
    for value in values {
        let id = required_string(value, "resource_id", "ontology resource")?;
        ensure!(valid_id(id), "invalid_resource_id:{id}");
        ensure!(ids.insert(id), "duplicate_resource_id:{id}");
        let kind = required_string(value, "kind", "ontology resource")?;
        ensure!(
            RESOURCE_KINDS.contains(&kind),
            "invalid_resource_kind:{id}:{kind}"
        );
        let display_name =
            required_string(value, "display_name", "ontology resource")?;
        let attributes = value
            .get("attributes")
            .and_then(Value::as_object)
            .context("ontology resource attributes missing")?;
        let references = string_array(value.get("references"), "references")?;
        let mut parents = string_array(attributes.get("extends"), "extends")?;
        parents
            .extend(string_array(attributes.get("interfaces"), "interfaces")?);
        resources.push(Resource {
            id,
            kind,
            display_name,
            owner: value.get("owner_id").and_then(Value::as_str),
            value_type: value.get("value_type").and_then(Value::as_str),
            references,
            parents,
            attributes,
        });
    }
    Ok(resources)
}

fn string_array<'a>(
    value: Option<&'a Value>,
    field: &str,
) -> Result<Vec<&'a str>> {
    match value {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::String(item)) => Ok(vec![item.as_str()]),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .with_context(|| format!("{field} must contain strings"))
            })
            .collect(),
        Some(_) => bail!("{field} must be a string or array"),
    }
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

fn valid_id(value: &str) -> bool {
    !value.is_empty() &&
        value.len() <= 128 &&
        value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() ||
                matches!(byte, b'_' | b'-' | b'.' | b':')
        })
}

fn validate_link_endpoint(
    resource: &Resource<'_>,
    field: &str,
    by_id: &BTreeMap<&str, &Resource<'_>>,
    issues: &mut Vec<String>,
) {
    let endpoint = resource.attributes.get(field).and_then(Value::as_str);
    match endpoint.and_then(|id| by_id.get(id).copied()) {
        None => issues.push(format!("dangling_link_{field}:{}", resource.id)),
        Some(target)
            if !matches!(target.kind, "object_type" | "event_type") =>
        {
            issues.push(format!("incompatible_link_{field}:{}", resource.id));
        },
        Some(_) => {},
    }
}

fn detect_cycles(
    resources: &[Resource<'_>],
    by_id: &BTreeMap<&str, &Resource<'_>>,
    issues: &mut Vec<String>,
) {
    if let Some(resource_id) = find_cycle(resources, by_id, false) {
        issues.push(format!("inheritance_cycle:{resource_id}"));
    }
    if let Some(resource_id) = find_cycle(resources, by_id, true) {
        issues.push(format!("owner_cycle:{resource_id}"));
    }
}

fn find_cycle<'a>(
    resources: &[Resource<'a>],
    by_id: &BTreeMap<&'a str, &Resource<'a>>,
    owner_edges: bool,
) -> Option<&'a str> {
    let mut state = BTreeMap::new();
    for resource in resources {
        if state.get(resource.id) == Some(&2_u8) {
            continue;
        }
        state.insert(resource.id, 1_u8);
        let mut pending = vec![(resource.id, 0_usize)];
        while let Some((current, edge_index)) = pending.last_mut() {
            let current_id = *current;
            let next = by_id.get(current_id).and_then(|current_resource| {
                if owner_edges {
                    (*edge_index == 0)
                        .then_some(current_resource.owner)
                        .flatten()
                } else {
                    current_resource.parents.get(*edge_index).copied()
                }
            });
            *edge_index = edge_index.saturating_add(1);
            let Some(next_id) = next else {
                state.insert(current_id, 2_u8);
                pending.pop();
                continue;
            };
            match state.get(next_id) {
                Some(1) => return Some(next_id),
                Some(2) => {},
                _ => {
                    state.insert(next_id, 1_u8);
                    pending.push((next_id, 0_usize));
                },
            }
        }
    }
    None
}

fn validate_inherited_properties(
    resources: &[Resource<'_>],
    by_id: &BTreeMap<&str, &Resource<'_>>,
    issues: &mut Vec<String>,
) {
    let mut properties: BTreeMap<&str, Vec<&Resource<'_>>> = BTreeMap::new();
    for resource in resources {
        if resource.kind == "property" &&
            let Some(owner) = resource.owner
        {
            properties.entry(owner).or_default().push(resource);
        }
    }
    for resource in resources {
        let Some(own_properties) = properties.get(resource.id) else {
            continue;
        };
        let ancestors = ancestors(resource.id, by_id);
        for ancestor in ancestors {
            let Some(inherited) = properties.get(ancestor) else {
                continue;
            };
            for own in own_properties {
                for parent in inherited {
                    if own.display_name == parent.display_name &&
                        own.value_type != parent.value_type
                    {
                        issues.push(format!(
                            "incompatible_property_type:{}:{}",
                            own.id, parent.id
                        ));
                    }
                }
            }
        }
    }
}

fn ancestors<'a>(
    id: &'a str,
    by_id: &BTreeMap<&'a str, &Resource<'a>>,
) -> BTreeSet<&'a str> {
    let mut result = BTreeSet::new();
    let mut pending = vec![id];
    while let Some(current) = pending.pop() {
        let Some(resource) = by_id.get(current) else {
            continue;
        };
        for parent in &resource.parents {
            if result.insert(*parent) {
                pending.push(*parent);
            }
        }
    }
    result
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

fn resource_values(ontology: &Value) -> Result<BTreeMap<&str, &Value>> {
    validate_ontology(ontology)?;
    ontology
        .get("resources")
        .and_then(Value::as_array)
        .context("ontology_resources_missing")?
        .iter()
        .map(|resource| {
            Ok((
                required_string(resource, "resource_id", "ontology resource")?,
                resource,
            ))
        })
        .collect()
}

fn diff_value(
    resource_id: &str,
    path: &[String],
    before: &Value,
    after: &Value,
    changes: &mut Vec<SemanticChange>,
) {
    if before == after {
        return;
    }
    if let (Some(before_object), Some(after_object)) =
        (before.as_object(), after.as_object())
    {
        let keys: BTreeSet<&str> = before_object
            .keys()
            .chain(after_object.keys())
            .map(String::as_str)
            .collect();
        for key in keys {
            if path.is_empty() && key == "resource_id" {
                continue;
            }
            let mut child_path = path.to_vec();
            child_path.push(key.to_owned());
            match (before_object.get(key), after_object.get(key)) {
                (Some(left), Some(right)) => {
                    diff_value(resource_id, &child_path, left, right, changes);
                },
                (left, right) => changes.push(SemanticChange {
                    resource_id: resource_id.to_owned(),
                    operation: if right.is_some() {
                        "set_field"
                    } else {
                        "remove_field"
                    }
                    .to_owned(),
                    path: child_path,
                    before: left.cloned(),
                    after: right.cloned(),
                }),
            }
        }
        return;
    }
    changes.push(SemanticChange {
        resource_id: resource_id.to_owned(),
        operation: "set_field".to_owned(),
        path: path.to_vec(),
        before: Some(before.clone()),
        after: Some(after.clone()),
    });
}

fn merge_value(
    resource_id: &str,
    path: &[String],
    base: Option<&Value>,
    left: Option<&Value>,
    right: Option<&Value>,
    conflicts: &mut Vec<MergeConflict>,
) -> Option<Value> {
    if left == right {
        return left.cloned();
    }
    if left == base {
        return right.cloned();
    }
    if right == base {
        return left.cloned();
    }
    if let (Some(base_object), Some(left_object), Some(right_object)) = (
        base.and_then(Value::as_object),
        left.and_then(Value::as_object),
        right.and_then(Value::as_object),
    ) {
        let keys: BTreeSet<&str> = base_object
            .keys()
            .chain(left_object.keys())
            .chain(right_object.keys())
            .map(String::as_str)
            .collect();
        let mut merged = Map::new();
        for key in keys {
            let mut child_path = path.to_vec();
            child_path.push(key.to_owned());
            if let Some(value) = merge_value(
                resource_id,
                &child_path,
                base_object.get(key),
                left_object.get(key),
                right_object.get(key),
                conflicts,
            ) {
                merged.insert(key.to_owned(), value);
            }
        }
        return Some(Value::Object(merged));
    }
    conflicts.push(MergeConflict {
        resource_id: resource_id.to_owned(),
        path: path.to_vec(),
        base: base.cloned(),
        left: left.cloned(),
        right: right.cloned(),
    });
    left.cloned()
}
