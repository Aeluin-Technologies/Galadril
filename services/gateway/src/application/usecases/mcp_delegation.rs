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
