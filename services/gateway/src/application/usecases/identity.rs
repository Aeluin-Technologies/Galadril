//! Identity verification use cases.

use std::sync::Arc;

use anyhow::{Result, bail};

use crate::application::ports::user_directory::{UserDirectory, UserStatus};

pub struct IdentityService {
    users: Arc<dyn UserDirectory>,
}

impl IdentityService {
    /// Create a new [`IdentityService`].
    pub fn new(users: Arc<dyn UserDirectory>) -> Self {
        Self { users }
    }

    pub async fn verify_user(
        &self,
        tenant_id: &str,
        user_id: &str,
    ) -> Result<()> {
        match self.users.get_user_status(tenant_id, user_id).await? {
            UserStatus::Active => Ok(()),
            UserStatus::Disabled => bail!("User '{user_id}' is disabled"),
            UserStatus::NotFound => bail!("User '{user_id}' not found"),
        }
    }
}

#[cfg(test)]
mod tests {
    use anyhow::Result;

    use super::*;

    struct FakeDir {
        status: UserStatus,
    }

    #[async_trait::async_trait]
    impl UserDirectory for FakeDir {
        async fn get_user_status(
            &self,
            _tenant_id: &str,
            _user_id: &str,
        ) -> Result<UserStatus> {
            Ok(self.status)
        }
    }

    #[tokio::test]
    async fn verify_user_active_ok() {
        let svc = IdentityService::new(Arc::new(FakeDir {
            status: UserStatus::Active,
        }));
        assert!(svc.verify_user("t1", "u1").await.is_ok());
    }

    #[tokio::test]
    async fn verify_user_disabled_err() {
        let svc = IdentityService::new(Arc::new(FakeDir {
            status: UserStatus::Disabled,
        }));
        assert!(svc.verify_user("t1", "u1").await.is_err());
    }
}
