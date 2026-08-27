//! Gateway domain.

mod storage;
mod tenant;

pub use storage::*;
pub use tenant::validate_tenant_id;
