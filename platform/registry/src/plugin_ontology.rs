//! Tenant-local overlays over immutable plugin semantic packages.
use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

use crate::semantics::{slice_ontology, validate_ontology};

type Resources = BTreeMap<String, Value>;

fn resource_id(resource: &Value) -> Result<&str> {
    resource
        .get("resource_id")
        .and_then(Value::as_str)
        .context("resource ID missing")
}

fn package_resources(package: &Value) -> Result<Resources> {
    let modules = package
        .get("ontologies")
        .and_then(Value::as_array)
        .context("ontology modules missing")?;
    ensure!(
        !modules.is_empty() && modules.len() <= 32,
        "invalid ontology modules"
    );
    let mut resources = Resources::new();
    for module in modules {
        let module_id = module
            .get("id")
            .and_then(Value::as_str)
            .context("module identity missing")?;
        for item in module
            .get("resources")
            .and_then(Value::as_array)
            .context("ontology resources missing")?
        {
            let id = resource_id(item)?.to_owned();
            let mut resource = item.clone();
            let attributes = resource
                .get_mut("attributes")
                .and_then(Value::as_object_mut)
                .context("resource attributes missing")?;
            attributes.insert("plugin_module".into(), json!(module_id));
            attributes.insert(
                "plugin_module_version".into(),
                module
                    .get("version")
                    .cloned()
                    .context("module version missing")?,
            );
            ensure!(
                resources.insert(id, resource).is_none(),
                "duplicate plugin resource"
            );
        }
    }
    Ok(resources)
}

fn target_resources(
    target: Option<&Value>,
    mappings: &BTreeMap<String, String>,
) -> Result<Resources> {
    if mappings.is_empty() {
        return Ok(Resources::new());
    }
    let target = target.context("mapped target ontology missing")?;
    let ids = mappings.values().cloned().collect::<Vec<_>>();
    // Registry's canonical closure owns inheritance and owned-property
    // semantics.
    let slice = slice_ontology(target, &ids, &[], true)?;
    slice
        .get("resources")
        .and_then(Value::as_array)
        .context("target resources missing")?
        .iter()
        .map(|item| Ok((resource_id(item)?.to_owned(), item.clone())))
        .collect()
}

fn rebind_participant(
    resource: &mut Value,
    destination: &str,
    selected: &Value,
) -> Result<()> {
    ensure!(
        resource
            .pointer("/attributes/mapping_slot")
            .and_then(Value::as_bool) ==
            Some(true),
        "resource is not a mapping slot"
    );
    ensure!(
        resource.get("kind").and_then(Value::as_str) == Some("link_type"),
        "only participant relationships can be rebound"
    );
    ensure!(
        selected.get("kind").and_then(Value::as_str) == Some("object_type"),
        "participant target must be an object type"
    );
    let old = resource
        .pointer("/attributes/target_type")
        .and_then(Value::as_str)
        .context("link target missing")?
        .to_owned();
    resource
        .pointer_mut("/attributes/target_type")
        .context("link target missing")?
        .clone_from(&json!(destination));
    for reference in resource
        .get_mut("references")
        .and_then(Value::as_array_mut)
        .context("link references missing")?
    {
        if reference.as_str() == Some(&old) {
            *reference = json!(destination);
        }
    }
    Ok(())
}

/// Builds a closed tenant view, importing only explicitly selected types.
/// The caller must authorize and pin the tenant-owned target ontology.
pub fn materialize(
    package: &Value,
    target: Option<&Value>,
    mappings: &BTreeMap<String, String>,
) -> Result<Value> {
    ensure!(
        serde_json::to_vec(package)?.len() <= 512 * 1024,
        "plugin ontology package exceeds limit"
    );
    ensure!(mappings.len() <= 128, "too many ontology mappings");
    let mut resources = package_resources(package)?;
    validate_ontology(
        &json!({"version":package.get("version"),"resources":resources.values().collect::<Vec<_>>()}),
    )?;
    let external = target_resources(target, mappings)?;
    for (slot, destination) in mappings {
        let resource = resources
            .get_mut(slot)
            .context("unknown plugin mapping slot")?;
        rebind_participant(
            resource,
            destination,
            external
                .get(destination)
                .context("target resource unavailable")?,
        )?;
    }
    for (id, resource) in external {
        ensure!(
            resources.insert(id, resource).is_none(),
            "target conflicts with plugin resource"
        );
    }
    ensure!(
        resources.len() <= 1024,
        "ontology overlay exceeds resource limit"
    );
    let result = json!({"version":package.get("version"),"resources":resources.values().collect::<Vec<_>>(),
       "plugin_origin":{"package_id":package.get("id"),"plugin_id":package.get("plugin_id"),"plugin_version":package.get("plugin_version"),"package_version":package.get("version")},"participant_mappings":mappings});
    validate_ontology(&result)?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn module_provenance_and_order_are_stable_without_mutating_the_package()
    -> Result<()> {
        let package: Value = serde_json::from_str(include_str!(
            "../../../plugins/src/galadril_plugins/utils/mail-ontology.json"
        ))?;
        let before = package.clone();
        let original = materialize(&package, None, &BTreeMap::new())?;
        let message = original
            .get("resources")
            .and_then(Value::as_array)
            .and_then(|items| {
                items.iter().find(|item| {
                    item.get("resource_id").and_then(Value::as_str) ==
                        Some("mail.message")
                })
            })
            .context("message missing")?;
        assert_eq!(
            message
                .pointer("/attributes/plugin_module")
                .and_then(Value::as_str),
            Some("utils.messages")
        );
        let mut reordered = package.clone();
        if let Some(modules) = reordered
            .get_mut("ontologies")
            .and_then(Value::as_array_mut)
        {
            modules.reverse();
            for module in modules {
                if let Some(resources) =
                    module.get_mut("resources").and_then(Value::as_array_mut)
                {
                    resources.reverse();
                }
            }
        }
        assert_eq!(materialize(&reordered, None, &BTreeMap::new())?, original);
        assert_eq!(package, before);
        Ok(())
    }

    #[test]
    fn required_block_binding_cannot_be_published_missing() -> Result<()> {
        let pipeline = json!({"sources":[{"id":"source"}],"pipeline":[{"step":"reply","type":"sink","input_from":["source"],"ontology_required":true}]});
        assert!(crate::semantics::validate_pipeline(&pipeline, &[]).is_err());
        crate::semantics::validate_pipeline(
            &pipeline,
            &[crate::semantics::PipelineBinding {
                block_id: "reply",
                ontology_id: "tenant-mail",
            }],
        )?;
        Ok(())
    }
    #[test]
    fn plugin_is_isolated_until_explicit_tenant_mapping() -> Result<()> {
        let mut package: Value = serde_json::from_str(include_str!(
            "../../../plugins/src/galadril_plugins/utils/mail-ontology.json"
        ))?;
        package
            .as_object_mut()
            .context("package object")?
            .insert("plugin_id".into(), json!("utils"));
        let isolated = materialize(&package, None, &BTreeMap::new())?;
        assert!(
            isolated
                .get("resources")
                .context("resources")?
                .as_array()
                .context("resources")?
                .iter()
                .all(|r| r.get("resource_id").and_then(Value::as_str) !=
                    Some("crm.person"))
        );
        let target = json!({"version":"v1", "resources":[{"resource_id":"crm.person","kind":"object_type","display_name":"Person","references":[],"attributes":{}},{"resource_id":"crm.person.name","kind":"property","display_name":"Name","owner_id":"crm.person","value_type":"string","references":[],"attributes":{}}]});
        let mappings =
            BTreeMap::from([("mail.message.to".into(), "crm.person".into())]);
        let linked = materialize(&package, Some(&target), &mappings)?;
        let slice = crate::semantics::slice_ontology(
            &linked,
            &[],
            &["object_type".into(), "property".into(), "link_type".into()],
            true,
        )?;
        assert_eq!(slice.get("resources"), linked.get("resources"));
        let values = linked
            .get("resources")
            .and_then(Value::as_array)
            .context("resources")?;
        assert!(
            values
                .iter()
                .any(|r| r.get("resource_id").and_then(Value::as_str) ==
                    Some("crm.person.name"))
        );
        assert!(values.iter().any(|r| {
            r.get("resource_id").and_then(Value::as_str) ==
                Some("mail.message.to") &&
                r.pointer("/attributes/target_type").and_then(Value::as_str) ==
                    Some("crm.person") &&
                r.get("references") ==
                    Some(&json!(["mail.message", "crm.person"]))
        }));
        assert!(values.iter().any(|r| {
            r.get("resource_id").and_then(Value::as_str) ==
                Some("mail.message.sender") &&
                r.pointer("/attributes/target_type").and_then(Value::as_str) ==
                    Some("mail.person")
        }));
        assert!(materialize(&package, None, &mappings).is_err());
        assert!(
            materialize(
                &package,
                Some(&target),
                &BTreeMap::from([(
                    "mail.message.subject".into(),
                    "crm.person".into()
                )])
            )
            .is_err()
        );
        assert_eq!(
            target
                .get("resources")
                .and_then(Value::as_array)
                .context("resources")?
                .len(),
            2
        );
        Ok(())
    }
}
