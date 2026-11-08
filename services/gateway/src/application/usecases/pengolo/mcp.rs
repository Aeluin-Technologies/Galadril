//! MCP grants derive their identity and selection from a live generation.

use super::*;

impl PengoloService {
    #[tracing::instrument(skip_all)]
    pub(crate) async fn authorize_mcp(
        &self,
        actor: &Principal<'_>,
        generation: &Record,
        temporary: bool,
        capability_id: &str,
        resource: &str,
    ) -> Result<Vec<String>> {
        let chat = self
            .authorize_generation(actor, generation, temporary)
            .await?;
        ensure!(
            generation.owner_id == actor.user &&
                matches!(
                    generation.data.get("status").and_then(Value::as_str),
                    Some("pending" | "running")
                ),
            "MCP generation is no longer active"
        );
        let input: SendInput = parse(
            generation
                .data
                .get("input")
                .cloned()
                .context("Missing MCP generation input")?,
        )?;
        let project = if let Some(id) = &generation.project_id {
            Some(self.project(actor, id).await?)
        } else {
            None
        };
        let branch = execution_context(project.as_ref().unwrap_or(&chat))?;
        let catalog = self.agent.capabilities().await?;
        let branch = registered_context(&catalog, branch.as_deref())?;
        let (_, selected, _) = self
            .selected_agents(actor, &input, branch.as_deref())
            .await?;
        mcp_binding(
            &catalog,
            &selected,
            branch.as_deref(),
            capability_id,
            resource,
        )
    }
}

/// Derives authority from installed metadata after live generation checks.
fn mcp_binding(
    catalog: &CapabilityCatalog,
    selected: &[String],
    context: Option<&str>,
    capability_id: &str,
    resource: &str,
) -> Result<Vec<String>> {
    ensure!(
        selected.iter().any(|id| id == capability_id),
        "MCP was not selected for this generation"
    );
    let capability = catalog
        .capabilities
        .iter()
        .find(|item| item.id == capability_id)
        .context("MCP unavailable")?;
    ensure!(
        capability.kind == CapabilityKind::Mcp,
        "MCP scope unavailable"
    );
    ensure!(
        capability.execution ==
            crate::domain::capabilities::CapabilityExecution::Registered &&
            capability.plugin_id.is_some(),
        "MCP execution unavailable"
    );
    ensure!(
        catalog.execution_allowed(capability_id, context),
        "MCP context unavailable"
    );
    let binding = capability
        .parameters_schema
        .get("x-galadril-mcp")
        .context("MCP binding unavailable")?;
    ensure!(
        binding.get("resource").and_then(Value::as_str) == Some(resource),
        "MCP resource mismatch"
    );
    let scopes: Vec<String> = serde_json::from_value(
        binding
            .get("scopes")
            .cloned()
            .context("MCP scopes unavailable")?,
    )?;
    ensure!(
        !scopes.is_empty() &&
            scopes.len() <= 32 &&
            scopes
                .iter()
                .all(|scope| !scope.is_empty() && scope.len() <= 128),
        "Invalid MCP scopes"
    );
    Ok(scopes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selection_context_and_resource_all_constrain_mcp_authority()
    -> Result<()> {
        let catalog = CapabilityCatalog::parse(&serde_json::to_vec(
            &json!({
                "schema_version": 1,
                "capabilities": [{
                    "id": "fixture.mcp.research", "plugin_id": "fixture", "plugin_version": "1.0.0",
                    "execution": "registered", "kind": "mcp", "targets": ["scribe"],
                    "name": "Fixture", "description": "Public research",
                    "parameters_schema": {"type": "object", "x-galadril-mcp": {
                        "resource": "https://mcp.example/research/mcp", "scopes": ["research:read"]
                    }},
                    "execution_profile": {"requires_context": "fixture.context", "contexts": [
                        {"id": "fixture.context", "name": "Fixture"}
                    ]}
                }]
            }),
        )?)?;
        let selected = vec!["fixture.mcp.research".to_owned()];
        assert_eq!(
            mcp_binding(
                &catalog,
                &selected,
                Some("fixture.context"),
                "fixture.mcp.research",
                "https://mcp.example/research/mcp"
            )?,
            vec!["research:read"]
        );
        assert!(
            mcp_binding(
                &catalog,
                &[],
                Some("fixture.context"),
                "fixture.mcp.research",
                "https://mcp.example/research/mcp"
            )
            .is_err()
        );
        assert!(
            mcp_binding(
                &catalog,
                &selected,
                Some("fixture.other"),
                "fixture.mcp.research",
                "https://mcp.example/research/mcp"
            )
            .is_err()
        );
        assert!(
            mcp_binding(
                &catalog,
                &selected,
                Some("fixture.context"),
                "fixture.mcp.research",
                "https://foreign.example/research/mcp"
            )
            .is_err()
        );
        Ok(())
    }
}
