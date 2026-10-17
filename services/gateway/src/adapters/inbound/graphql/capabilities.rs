//! Typed public projections contain deployment metadata, never service
//! configuration.

use juniper::{FieldError, FieldResult, GraphQLEnum, GraphQLObject};

use crate::application::usecases::capabilities::DiscoveryError;
use crate::domain::capabilities::{
    CapabilityCatalog, CapabilityExecution, CapabilityKind, CapabilityOrigin,
    CapabilityTarget,
};

#[derive(Clone, Copy, GraphQLEnum)]
#[graphql(name = "CapabilityKind")]
pub enum Kind {
    Input,
    Output,
    Model,
    Mcp,
    Skill,
    Algorithm,
}

#[derive(GraphQLEnum)]
#[graphql(name = "CapabilityTarget")]
pub enum Target {
    Pipeline,
    Scribe,
}

impl From<Kind> for CapabilityKind {
    fn from(kind: Kind) -> Self {
        match kind {
            Kind::Input => Self::Input,
            Kind::Output => Self::Output,
            Kind::Model => Self::Model,
            Kind::Mcp => Self::Mcp,
            Kind::Skill => Self::Skill,
            Kind::Algorithm => Self::Algorithm,
        }
    }
}

#[derive(GraphQLEnum)]
#[graphql(name = "CapabilityOrigin")]
pub enum Origin {
    Plugin,
    User,
}

#[derive(GraphQLEnum)]
#[graphql(name = "CapabilityExecution")]
pub enum Execution {
    Registered,
    Declared,
}

#[derive(GraphQLObject)]
#[graphql(name = "CapabilityCompatibility")]
pub struct Compatibility {
    allowed_models: Vec<String>,
    requires: Vec<String>,
}

#[derive(GraphQLObject)]
#[graphql(name = "Capability")]
pub struct Capability {
    id: String,
    plugin_id: Option<String>,
    plugin_version: Option<String>,
    origin: Origin,
    execution: Execution,
    compatibility: Compatibility,
    kind: Kind,
    targets: Vec<Target>,
    name: String,
    description: String,
    parameters_schema_json: String,
    step_type: Option<String>,
    model_class: Option<String>,
}

#[derive(GraphQLObject)]
#[graphql(name = "CapabilityCatalog")]
pub struct Catalog {
    schema_version: i32,
    capabilities: Vec<Capability>,
}

pub fn discovery_error(error: DiscoveryError) -> FieldError {
    let code = match error {
        DiscoveryError::Forbidden => "FORBIDDEN",
        DiscoveryError::Unavailable => "CAPABILITY_CATALOG_UNAVAILABLE",
    };
    FieldError::new(error.to_string(), juniper::graphql_value!({"code": code}))
}

pub fn project(
    catalog: &CapabilityCatalog,
    target: CapabilityTarget,
    kind: Option<Kind>,
    plugin_id: Option<&str>,
) -> FieldResult<Catalog> {
    if plugin_id.is_some_and(|value| value.is_empty() || value.len() > 64) {
        return Err(FieldError::new(
            "Invalid plugin filter",
            juniper::graphql_value!({"code": "BAD_USER_INPUT"}),
        ));
    }
    let selected_kind = kind.map(CapabilityKind::from);
    let capabilities = catalog
        .capabilities
        .iter()
        .filter(|item| {
            item.targets.contains(&target) &&
                selected_kind.is_none_or(|kind| item.kind == kind) &&
                plugin_id
                    .is_none_or(|id| item.plugin_id.as_deref() == Some(id))
        })
        .map(|item| {
            Ok(Capability {
                id: item.id.clone(),
                plugin_id: item.plugin_id.clone(),
                plugin_version: item.plugin_version.clone(),
                origin: match item.origin {
                    CapabilityOrigin::Plugin => Origin::Plugin,
                    CapabilityOrigin::User => Origin::User,
                },
                execution: match item.execution {
                    CapabilityExecution::Registered => Execution::Registered,
                    CapabilityExecution::Declared => Execution::Declared,
                },
                compatibility: Compatibility {
                    allowed_models: item.compatibility.allowed_models.clone(),
                    requires: item.compatibility.requires.clone(),
                },
                kind: match item.kind {
                    CapabilityKind::Input => Kind::Input,
                    CapabilityKind::Output => Kind::Output,
                    CapabilityKind::Model => Kind::Model,
                    CapabilityKind::Mcp => Kind::Mcp,
                    CapabilityKind::Skill => Kind::Skill,
                    CapabilityKind::Algorithm => Kind::Algorithm,
                },
                targets: item
                    .targets
                    .iter()
                    .map(|target| match target {
                        CapabilityTarget::Pipeline => Target::Pipeline,
                        CapabilityTarget::Scribe => Target::Scribe,
                    })
                    .collect(),
                name: item.name.clone(),
                description: item.description.clone(),
                parameters_schema_json: serde_json::to_string(
                    &item.parameters_schema,
                )?,
                step_type: item.step_type.clone(),
                model_class: item.model_class.clone(),
            })
        })
        .collect::<FieldResult<Vec<_>>>()?;
    Ok(Catalog {
        schema_version: i32::from(catalog.schema_version),
        capabilities,
    })
}

#[cfg(test)]
mod tests {
    use anyhow::{Result, ensure};

    use super::*;

    struct Fixture;

    #[juniper::graphql_object]
    impl Fixture {
        fn catalog(
            kind: Option<Kind>,
            plugin_id: Option<String>,
        ) -> FieldResult<Catalog> {
            project(
                crate::domain::capabilities::builtin_catalog()?,
                CapabilityTarget::Pipeline,
                kind,
                plugin_id.as_deref(),
            )
        }

        fn user_catalog() -> FieldResult<Catalog> {
            let metadata = serde_json::json!({"schema_version":1,"capabilities":[{"id":"user.skill.research","origin":"user","kind":"skill","execution":"declared","targets":["scribe"],"name":"Research","description":"Workflow","parameters_schema":{"type":"object"},"compatibility":{"allowed_models":["legal"],"requires":["law.mcp.research"]}}]});
            let catalog =
                CapabilityCatalog::parse(&serde_json::to_vec(&metadata)?)?;
            project(
                &catalog,
                CapabilityTarget::Scribe,
                Some(Kind::Skill),
                None,
            )
        }

        fn denied() -> FieldResult<Option<Catalog>> {
            Err(discovery_error(DiscoveryError::Forbidden))
        }

        fn unavailable() -> FieldResult<Option<Catalog>> {
            Err(discovery_error(DiscoveryError::Unavailable))
        }
    }

    #[test]
    fn capabilities_selected_fields_and_filters_preserve_public_contract()
    -> Result<()> {
        let schema = juniper::RootNode::new(
            Fixture,
            juniper::EmptyMutation::<()>::new(),
            juniper::EmptySubscription::<()>::new(),
        );
        let (result, errors) = juniper::execute_sync(
            "{ catalog(kind: INPUT, pluginId: \"core\") { schemaVersion capabilities { id kind targets parametersSchemaJson } } }",
            None,
            &schema,
            &juniper::Variables::new(),
            &(),
        )?;
        ensure!(errors.is_empty());
        let value = serde_json::to_value(result)?;
        let items = value
            .pointer("/catalog/capabilities")
            .and_then(serde_json::Value::as_array)
            .ok_or_else(|| anyhow::anyhow!("Missing catalog projection"))?;
        ensure!(items.len() == 1);
        ensure!(
            items
                .first()
                .and_then(|item| item.get("id"))
                .and_then(serde_json::Value::as_str) ==
                Some("core.input.kafka")
        );
        ensure!(
            project(
                crate::domain::capabilities::builtin_catalog()?,
                CapabilityTarget::Pipeline,
                None,
                Some("missing")
            )
            .map_err(|error| anyhow::anyhow!("{error:?}"))?
            .capabilities
            .is_empty()
        );
        Ok(())
    }

    #[test]
    fn capabilities_user_skill_projection_exposes_constraints() -> Result<()> {
        let schema = juniper::RootNode::new(
            Fixture,
            juniper::EmptyMutation::<()>::new(),
            juniper::EmptySubscription::<()>::new(),
        );
        let (result, errors) = juniper::execute_sync(
            "{ userCatalog { capabilities { id pluginId pluginVersion origin kind execution compatibility { allowedModels requires } } } }",
            None,
            &schema,
            &juniper::Variables::new(),
            &(),
        )?;
        ensure!(errors.is_empty());
        let value = serde_json::to_value(result)?;
        ensure!(
            value
                .pointer("/userCatalog/capabilities/0/pluginId")
                .is_some_and(serde_json::Value::is_null)
        );
        ensure!(
            value
                .pointer("/userCatalog/capabilities/0/origin")
                .and_then(serde_json::Value::as_str) ==
                Some("USER")
        );
        ensure!(
            value
                .pointer("/userCatalog/capabilities/0/kind")
                .and_then(serde_json::Value::as_str) ==
                Some("SKILL")
        );
        ensure!(
            value
                .pointer("/userCatalog/capabilities/0/execution")
                .and_then(serde_json::Value::as_str) ==
                Some("DECLARED")
        );
        ensure!(
            value
                .pointer(
                    "/userCatalog/capabilities/0/compatibility/allowedModels/0"
                )
                .and_then(serde_json::Value::as_str) ==
                Some("legal")
        );
        ensure!(
            value
                .pointer(
                    "/userCatalog/capabilities/0/compatibility/requires/0"
                )
                .and_then(serde_json::Value::as_str) ==
                Some("law.mcp.research")
        );
        Ok(())
    }

    #[test]
    fn capabilities_errors_keep_codes_and_exact_graphql_paths() -> Result<()> {
        let schema = juniper::RootNode::new(
            Fixture,
            juniper::EmptyMutation::<()>::new(),
            juniper::EmptySubscription::<()>::new(),
        );
        let (_, errors) = juniper::execute_sync(
            "{ denied { schemaVersion } unavailable { schemaVersion } }",
            None,
            &schema,
            &juniper::Variables::new(),
            &(),
        )?;
        let errors = serde_json::to_value(errors)?;
        ensure!(
            errors
                .pointer("/0/path/0")
                .and_then(serde_json::Value::as_str) ==
                Some("denied")
        );
        ensure!(
            errors
                .pointer("/0/extensions/code")
                .and_then(serde_json::Value::as_str) ==
                Some("FORBIDDEN")
        );
        ensure!(
            errors
                .pointer("/1/path/0")
                .and_then(serde_json::Value::as_str) ==
                Some("unavailable")
        );
        ensure!(
            errors
                .pointer("/1/extensions/code")
                .and_then(serde_json::Value::as_str) ==
                Some("CAPABILITY_CATALOG_UNAVAILABLE")
        );
        Ok(())
    }
}
