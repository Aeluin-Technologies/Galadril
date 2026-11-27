//! Versioned resource selection and external actions from the registered
//! catalog.

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum ActionMode {
    Automatic,
    Approval,
    Draft,
}

pub(super) struct ExternalAction<'a> {
    pub target: &'a str,
    pub mode: ActionMode,
    pub decision_required: bool,
}

pub(super) struct ResourceSelection<'a> {
    pub action: Option<ExternalAction<'a>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding<'a> {
    version: u8,
    resource: &'a str,
    #[serde(borrow)]
    operation: Operation<'a>,
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Operation<'a> {
    Select { parameter: &'a str },
    Execute { parameter: &'a str, mode: &'a str },
    Decide,
}

fn registered_operation<'a>(
    node: &Value,
    resource: &str,
    resolve: &impl Fn(&str, Option<&str>) -> Option<&'a Value>,
) -> Option<Option<Operation<'a>>> {
    let Some(id) = node.get("capability_id").filter(|value| !value.is_null())
    else {
        return Some(None);
    };
    let version = node.get("capability_version").and_then(Value::as_str);
    let schema = resolve(id.as_str()?, version)?;
    let Some(value) = schema.get("x-galadril-resource-binding") else {
        return Some(None);
    };
    let binding = Binding::deserialize(value).ok()?;
    if binding.version != 1 || version.is_none() {
        return None;
    }
    Some((binding.resource == resource).then_some(binding.operation))
}

/// Keeps collection independent when the effect chain is absent or
/// unresolvable.
pub(super) fn authorize<'a, 'catalog>(
    definition: &'a Value,
    resource: &str,
    selected: &'a str,
    resolve: impl Fn(&str, Option<&str>) -> Option<&'catalog Value>,
) -> Option<ResourceSelection<'a>> {
    let matches =
        definition.get("sources")?.as_array()?.iter().any(|source| {
            let Some(Some(Operation::Select { parameter })) =
                registered_operation(source, resource, &resolve)
            else {
                return false;
            };
            source.get(parameter).and_then(Value::as_str) == Some(selected)
        });
    if !matches {
        return None;
    }
    Some(ResourceSelection {
        action: external_action(definition, resource, selected, &resolve),
    })
}

fn external_action<'a, 'catalog>(
    definition: &'a Value,
    resource: &str,
    selected: &'a str,
    resolve: &impl Fn(&str, Option<&str>) -> Option<&'catalog Value>,
) -> Option<ExternalAction<'a>> {
    let mut execution = None;
    let mut decision_required = false;
    for step in definition.get("pipeline")?.as_array()? {
        // An unavailable step could have provided a mandatory decision.
        match registered_operation(step, resource, resolve)? {
            Some(Operation::Execute { parameter, mode }) => {
                if execution.is_some() {
                    return None;
                }
                let params = step.get("params")?;
                let target = params.get(parameter)?.as_str()?;
                let mode = ActionMode::deserialize(params.get(mode)?).ok()?;
                execution = Some((
                    if target == "source" { selected } else { target },
                    mode,
                ));
            },
            Some(Operation::Decide) => decision_required = true,
            _ => {},
        }
    }
    let (target, mode) = execution?;
    Some(ExternalAction {
        target,
        mode,
        decision_required,
    })
}
