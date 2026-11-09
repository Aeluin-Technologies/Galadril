//! Attenuated MCP grants never carry the parent generation capability.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use uuid::Uuid;

pub struct McpContext {
    pub tenant: String,
    pub user: String,
    pub generation: String,
    pub temporary: bool,
    pub capability: String,
    pub resource: String,
    pub expires: Instant,
    pub parent: String,
}

#[derive(Default)]
pub struct McpDelegations {
    grants: Mutex<HashMap<String, Arc<McpContext>>>,
}

impl McpDelegations {
    pub fn issue(&self, context: McpContext) -> Result<String> {
        let now = Instant::now();
        ensure!(
            context.expires > now &&
                context.expires <= now + Duration::from_secs(90),
            "Invalid MCP lifetime"
        );
        ensure!(
            context.capability.len() <= 128 && context.resource.len() <= 2048,
            "Invalid MCP scope"
        );
        let mut grants = self.grants.lock().map_err(|_| {
            anyhow::anyhow!("MCP delegation registry unavailable")
        })?;
        grants.retain(|_, entry| entry.expires > now);
        if let Some((token, _)) = grants.iter().find(|(_, entry)| {
            entry.expires > now + Duration::from_secs(5) &&
                entry.parent == context.parent &&
                entry.capability == context.capability &&
                entry.resource == context.resource &&
                entry.user == context.user &&
                entry.tenant == context.tenant &&
                entry.generation == context.generation &&
                entry.temporary == context.temporary
        }) {
            return Ok(token.clone());
        }
        ensure!(grants.len() < 256, "MCP delegation capacity exceeded");
        let token =
            format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        grants.insert(token.clone(), Arc::new(context));
        Ok(token)
    }

    pub fn resolve(
        &self,
        token: &str,
        capability: &str,
        resource: &str,
    ) -> Result<Arc<McpContext>> {
        let grants = self.grants.lock().map_err(|_| {
            anyhow::anyhow!("MCP delegation registry unavailable")
        })?;
        let grant = grants
            .get(token)
            .filter(|entry| {
                entry.expires > Instant::now() &&
                    entry.capability == capability &&
                    entry.resource == resource
            })
            .context("MCP delegation denied")?;
        Ok(Arc::clone(grant))
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use anyhow::Result;

    use super::{McpContext, McpDelegations};

    #[test]
    fn repeated_tools_reuse_live_authority_instead_of_exhausting_the_registry()
    -> Result<()> {
        let grants = McpDelegations::default();
        let scope = || McpContext {
            tenant: "tenant".into(),
            user: "user".into(),
            generation: "run".into(),
            temporary: false,
            capability: "core.mcp.workspace".into(),
            resource: "https://mcp.example/workspace/mcp".into(),
            expires: Instant::now() + Duration::from_secs(60),
            parent: "parent".into(),
        };
        let token = grants.issue(scope())?;
        for _ in 0..512 {
            assert_eq!(grants.issue(scope())?, token);
        }
        Ok(())
    }

    #[test]
    fn delegated_token_is_bound_to_user_mcp_resource_and_expiration()
    -> Result<()> {
        let grants = McpDelegations::default();
        let token = grants.issue(McpContext {
            tenant: "tenant_a".into(),
            user: "user_a".into(),
            generation: "generation_a".into(),
            temporary: false,
            capability: "fixture.mcp.research".into(),
            resource: "https://mcp.example/research".into(),
            expires: Instant::now() + Duration::from_secs(30),
            parent: "generation-only-authority".into(),
        })?;
        let context = grants.resolve(
            &token,
            "fixture.mcp.research",
            "https://mcp.example/research",
        )?;
        assert_eq!(context.tenant, "tenant_a");
        assert_eq!(context.user, "user_a");
        assert!(
            grants
                .resolve(
                    &token,
                    "foreign.mcp.research",
                    "https://mcp.example/research"
                )
                .is_err()
        );
        assert!(
            grants
                .resolve(
                    &token,
                    "fixture.mcp.research",
                    "https://foreign.example"
                )
                .is_err()
        );
        assert!(
            grants
                .resolve(
                    "forged",
                    "fixture.mcp.research",
                    "https://mcp.example/research"
                )
                .is_err()
        );
        Ok(())
    }
}
