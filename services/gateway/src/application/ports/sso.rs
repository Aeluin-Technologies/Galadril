//! Secret-free tenant metadata and private identity broker ports.

use anyhow::Result;

use crate::domain::sso::{ConnectionInput, ProviderDiscovery, SsoSettings};

pub struct Organization {
    pub tenant_id: String,
    pub owner_id: String,
    pub ready: bool,
    pub enrollment_token: String,
}

#[async_trait::async_trait]
pub trait SsoStore: Send + Sync {
    /// Reserves stable organization identifiers for an idempotent provisioner.
    async fn reserve(
        &self,
        request_id: &str,
        domain: &str,
        external_subject: &str,
    ) -> Result<Organization>;
    /// Returns only non-secret administrative metadata.
    async fn settings(&self, tenant: &str) -> Result<SsoSettings>;
    /// Validates account status and the current tenant second-factor
    /// generation.
    async fn session_allowed(
        &self,
        tenant: &str,
        user: &str,
        assurance: Option<&str>,
        generation: Option<i64>,
    ) -> Result<bool>;
    /// Persists organization display metadata without changing its immutable
    /// identity.
    async fn organization_name(&self, tenant: &str, name: &str) -> Result<()>;
    /// Persists a short-lived ownership challenge without claiming the domain.
    async fn challenge(
        &self,
        tenant: &str,
        domain: &str,
        proof: &str,
    ) -> Result<()>;
    /// Loads this tenant's unexpired domain ownership challenge.
    async fn proof(&self, tenant: &str, domain: &str) -> Result<String>;
    /// Reserves verified routing ownership before changing the broker.
    async fn claim_domain(
        &self,
        tenant: &str,
        domain: &str,
    ) -> Result<Option<String>>;
    /// Publishes a verified connection reference without persisting secrets.
    async fn connection(
        &self,
        tenant: &str,
        input: &ConnectionInput,
        connection_id: &str,
    ) -> Result<()>;
    /// Updates the tenant's explicit permission import opt-in.
    async fn import_setting(&self, tenant: &str, enabled: bool) -> Result<()>;
    /// Changes the explicit organization-wide second-factor requirement.
    async fn passkey_setting(&self, tenant: &str, enabled: bool)
    -> Result<()>;
    /// Invalidates credentials and pending proofs in one tenant transaction.
    async fn reset_passkeys(&self, tenant: &str, user: &str) -> Result<()>;
    /// Disables discovery without removing identity bindings.
    async fn disable(&self, tenant: &str, domain: &str) -> Result<()>;
    /// Links a provider subject to an existing account without email matching.
    async fn link_identity(
        &self,
        tenant: &str,
        domain: &str,
        subject: &str,
        user: &str,
    ) -> Result<()>;
    /// Enrolls the provisioned owner without linking accounts by email.
    async fn owner(
        &self,
        organization: &Organization,
        connection: &str,
        external_subject: &str,
    ) -> Result<()>;
    /// Publishes the organization after all authorization stores are ready.
    async fn complete(&self, organization: &Organization) -> Result<()>;
    /// Releases only this attempt's lease so failed enrollments can retry.
    async fn release(&self, organization: &Organization) -> Result<()>;
}

#[async_trait::async_trait]
pub trait SsoBroker: Send + Sync {
    /// Inspects public metadata without registering clients or changing
    /// routing.
    async fn detect(&self, address: &str) -> Result<ProviderDiscovery>;
    /// Creates or updates the tenant/domain connection in encrypted Polis
    /// storage.
    async fn configure(
        &self,
        tenant: &str,
        input: &ConnectionInput,
        existing_id: Option<&str>,
    ) -> Result<String>;
    /// Verifies a DNS TXT proof through a fixed authenticated resolver.
    async fn verify_domain(&self, domain: &str, proof: &str) -> Result<()>;
    /// Initializes the organization's canonical Registry storage.
    async fn initialize_organization(&self, tenant: &str) -> Result<()>;
}
