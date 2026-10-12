//! Tenant-bounded access tuples used by administration and graph inspection.

use anyhow::Result;
use serde::Serialize;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessTuple {
    pub resource_type: String,
    pub resource_id: String,
    pub relation: String,
    pub subject_type: String,
    pub subject_id: String,
    pub subject_relation: String,
}

#[async_trait::async_trait]
pub trait AccessStore: Send + Sync {
    /// Reads fully consistent canonical tuples within one tenant namespace.
    async fn tuples(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<AccessTuple>>;

    /// Anchors a bounded read to one tenant-visible principal before scanning.
    async fn tuples_for_subject(
        &self,
        _: &str,
        _: &str,
        _: &str,
        _: usize,
    ) -> Result<Vec<AccessTuple>> {
        anyhow::bail!("Indexed access search is unavailable")
    }

    /// Commits one validated subject or subject-set relationship.
    async fn set(
        &self,
        tenant_id: &str,
        tuple: &AccessTuple,
        enabled: bool,
    ) -> Result<()>;

    /// Publishes a bounded lifecycle cleanup in a single native transaction.
    async fn set_many(
        &self,
        tenant_id: &str,
        tuples: &[AccessTuple],
        enabled: bool,
    ) -> Result<()> {
        anyhow::ensure!(
            tuples.len() <= 128,
            "Access changes must contain at most 128 relationships"
        );
        for tuple in tuples {
            self.set(tenant_id, tuple, enabled).await?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::application::usecases::iam_admin::validate_resource_access;

    #[test]
    fn resource_access_never_accepts_structural_or_cross_tenant_edges() {
        let mut tuple = AccessTuple {
            resource_type: "document".into(),
            resource_id: "reports/annual.pdf".into(),
            relation: "reader".into(),
            subject_type: "role".into(),
            subject_id: "analyst".into(),
            subject_relation: "member".into(),
        };
        assert!(validate_resource_access(&tuple).is_ok());
        tuple.relation = "parent".into();
        assert!(validate_resource_access(&tuple).is_err());
        tuple.relation = "reader".into();
        tuple.subject_id = "other/analyst".into();
        assert!(validate_resource_access(&tuple).is_err());
        tuple.subject_id = "analyst".into();
        tuple.resource_id = "../private".into();
        assert!(validate_resource_access(&tuple).is_err());
        tuple.resource_id = "report".into();
        tuple.resource_type = "tenant".into();
        assert!(validate_resource_access(&tuple).is_err());
    }
}
