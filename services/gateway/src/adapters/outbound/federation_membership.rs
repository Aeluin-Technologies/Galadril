//! Federation projects approved memberships through the canonical
//! authorization adapter.

use std::sync::Arc;

use anyhow::Result;

use crate::application::usecases::authorization::Authorization;
use crate::authentication::MembershipWriter;
use crate::domain::validate_tenant_id;

pub struct FederationMembershipWriter(pub Arc<dyn Authorization>);

#[async_trait::async_trait]
impl MembershipWriter for FederationMembershipWriter {
    async fn role_membership(
        &self,
        tenant: &str,
        user: &str,
        role: &str,
        enabled: bool,
    ) -> Result<()> {
        let tenant = validate_tenant_id(tenant)?;
        let resource = format!("{tenant}/{role}");
        if enabled {
            self.0
                .upsert_relationship("role", &resource, "member", "user", user)
                .await
        } else {
            self.0
                .delete_relationship("role", &resource, "member", "user", user)
                .await
        }
    }

    async fn tenant_membership(&self, tenant: &str, user: &str) -> Result<()> {
        let tenant = validate_tenant_id(tenant)?;
        self.0
            .upsert_relationship("tenant", tenant, "member", "user", user)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::application::test_support::{
        AuthorizationDecision, TestAuthorization,
    };

    #[tokio::test]
    async fn federation_projects_tenant_scoped_roles_and_propagates_revocation()
    -> Result<()> {
        let auth = TestAuthorization::new(AuthorizationDecision::Allow);
        let writer = FederationMembershipWriter(
            Arc::clone(&auth) as Arc<dyn Authorization>
        );
        writer
            .role_membership("acme", "alice", "analyst", true)
            .await?;
        writer
            .role_membership("acme", "alice", "analyst", false)
            .await?;
        writer.tenant_membership("acme", "alice").await?;
        let mutations = auth
            .mutations
            .lock()
            .map_err(|error| anyhow::anyhow!("test lock poisoned: {error}"))?;
        assert_eq!(mutations.len(), 3);
        let granted = mutations
            .first()
            .ok_or_else(|| anyhow::anyhow!("missing grant"))?;
        assert_eq!(granted.resource_type, "role");
        assert_eq!(granted.resource_id, "acme/analyst");
        assert_eq!(granted.subject_id, "alice");
        let revoked = mutations
            .get(1)
            .ok_or_else(|| anyhow::anyhow!("missing revoke"))?;
        assert_eq!(revoked.operation, "delete");
        assert_eq!(revoked.resource_id, granted.resource_id);
        let member = mutations
            .last()
            .ok_or_else(|| anyhow::anyhow!("missing membership"))?;
        assert_eq!(member.resource_type, "tenant");
        assert_eq!(member.resource_id, "acme");
        assert_eq!(member.relation, "member");
        Ok(())
    }
}
