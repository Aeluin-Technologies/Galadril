//! Gateway domain.

pub mod agent_activity;

pub mod capabilities;
pub mod pengolo;
pub mod sso;
mod storage;
mod tenant;

pub use storage::*;
pub use tenant::validate_tenant_id;
