//! Runs the real upload use case without linking unrelated Gateway runtimes.

#[path = "../src/application/usecases/audit.rs"]
pub mod audit;
#[path = "../src/application/ports/audit_store.rs"]
pub mod audit_store;
#[path = "../src/application/usecases/identity.rs"]
pub mod identity;
#[path = "../src/domain/storage/key.rs"]
pub mod key;
#[path = "../src/application/test_support.rs"]
pub mod test_support;
#[path = "../src/application/ports/upload_store.rs"]
pub mod upload_store;
#[path = "../src/application/usecases/uploads.rs"]
pub mod uploads;
#[path = "../src/application/ports/user_directory.rs"]
pub mod user_directory;

pub mod domain {
    pub use crate::key;
}

pub mod application {
    pub use crate::test_support;
    pub mod ports {
        pub use crate::{audit_store, upload_store, user_directory};
    }
    pub mod usecases {
        pub use crate::{audit, identity, uploads};
        pub mod authorization {
            #[derive(Debug, Default, Clone)]
            pub struct QueryContext {
                pub request_id: String,
                pub trace_id: Option<String>,
            }
            #[derive(Debug, Clone, Copy, PartialEq, Eq)]
            pub enum Permission {
                Ingest,
            }
            #[async_trait::async_trait]
            pub trait Authorization: Send + Sync {
                async fn upsert_relationship(
                    &self,
                    resource_type: &str,
                    resource_id: &str,
                    relation: &str,
                    subject_type: &str,
                    subject_id: &str,
                ) -> anyhow::Result<()>;
                async fn delete_relationship(
                    &self,
                    resource_type: &str,
                    resource_id: &str,
                    relation: &str,
                    subject_type: &str,
                    subject_id: &str,
                ) -> anyhow::Result<()>;
                async fn is_authorized(
                    &self,
                    user: &str,
                    tenant: &str,
                    permission: Permission,
                    resource_type: &str,
                    resource_id: &str,
                    context: Option<&QueryContext>,
                ) -> anyhow::Result<bool>;
                async fn invalidate_tenant_cache(&self, tenant: &str);
            }
        }
    }
}
