//! Private authentication persistence shared by HTTP adapters and integration
//! tests.

mod api;
pub(crate) mod federation;
mod store;

pub use api::router_with_membership;
pub use federation::{FederationConfig, MembershipWriter};
