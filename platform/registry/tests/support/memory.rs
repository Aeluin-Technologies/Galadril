//! An immutable in-memory revision store for Registry behavior tests.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, ensure};
use async_trait::async_trait;
use galadril_registry::state::TenantState;
use galadril_registry::storage::{
    RevisionStore, Snapshot, TenantPage, TenantSnapshot,
};

#[derive(Default, Clone)]
pub struct MemoryStore(
    Arc<Mutex<BTreeMap<String, Vec<(String, TenantState)>>>>,
);

#[async_trait]
impl RevisionStore for MemoryStore {
    async fn list_tenants(
        &self,
        limit: u32,
        page: Option<&str>,
    ) -> Result<TenantPage> {
        let tenants = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test store"))?;
        Ok(TenantPage {
            tenant_ids: tenants
                .keys()
                .filter(|key| page.is_none_or(|page| key.as_str() > page))
                .take(usize::try_from(limit)?)
                .cloned()
                .collect(),
            next_page_token: None,
        })
    }

    async fn tenant_exists(&self, tenant: &str) -> Result<bool> {
        Ok(self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test store"))?
            .contains_key(tenant))
    }

    async fn ensure_tenant(&self, tenant: &str) -> Result<TenantSnapshot> {
        let mut tenants = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test store"))?;
        let snapshots = tenants.entry(tenant.into()).or_default();
        Ok(TenantSnapshot {
            revision_id: snapshots
                .last()
                .map_or_else(|| "0".repeat(20), |(id, _)| id.clone()),
        })
    }

    async fn delete_tenant(&self, tenant: &str) -> Result<()> {
        self.0
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test store"))?
            .remove(tenant)
            .context("tenant unavailable")?;
        Ok(())
    }

    async fn load(
        &self,
        tenant: &str,
        revision: Option<&str>,
    ) -> Result<Snapshot> {
        let tenants = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test store"))?;
        let snapshots = tenants.get(tenant).context("tenant unavailable")?;
        let selected = if let Some(revision) = revision {
            snapshots
                .iter()
                .find(|(id, _)| id == revision)
                .context("revision unavailable")?
        } else if let Some(snapshot) = snapshots.last() {
            snapshot
        } else {
            return Ok(Snapshot {
                revision_id: "0".repeat(20),
                state: TenantState::default(),
            });
        };
        Ok(Snapshot {
            revision_id: selected.0.clone(),
            state: selected.1.clone(),
        })
    }

    async fn commit(
        &self,
        tenant: &str,
        expected: &str,
        state: &TenantState,
        _author: &str,
        _message: &str,
    ) -> Result<String> {
        let mut tenants = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("poisoned test store"))?;
        let snapshots =
            tenants.get_mut(tenant).context("tenant unavailable")?;
        ensure!(
            snapshots
                .last()
                .map_or_else(|| "0".repeat(20), |(id, _)| id.clone()) ==
                expected,
            "registry revision changed"
        );
        let revision = format!("{:020}", snapshots.len() + 1);
        snapshots.push((revision.clone(), state.clone()));
        Ok(revision)
    }
}
