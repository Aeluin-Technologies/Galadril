//! Personal-installation callbacks and account owners cannot be selected by
//! clients.

use std::net::SocketAddr;

use anyhow::{Context, Result, ensure};
use sha2::{Digest, Sha256};

#[derive(Clone)]
pub struct CallbackConfig {
    pub bind: SocketAddr,
    pub redirect: String,
}

impl CallbackConfig {
    pub fn parse(bind: &str) -> Result<Self> {
        let bind: SocketAddr =
            bind.parse().context("Invalid ChatGPT callback listener")?;
        ensure!(
            bind.is_ipv4() &&
                (bind.ip().is_loopback() || bind.ip().is_unspecified()) &&
                bind.port() >= 1024,
            "Invalid personal callback listener"
        );
        Ok(Self {
            bind,
            redirect: format!(
                "http://127.0.0.1:{}/auth/callback",
                bind.port()
            ),
        })
    }
}

pub fn owner_key(tenant: &str, user: &str) -> Result<String> {
    ensure!(
        [tenant, user].iter().all(|value| !value.is_empty() &&
            value.len() <= 256 &&
            !value.contains('\0')),
        "Invalid provider account scope"
    );
    let mut digest = Sha256::new();
    digest.update(b"galadril.chatgpt.owner.v1\0");
    digest.update(tenant.as_bytes());
    digest.update(b"\0");
    digest.update(user.as_bytes());
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owners_are_tenant_bound_and_unambiguous() -> anyhow::Result<()> {
        assert_ne!(
            owner_key("tenant-a", "user")?,
            owner_key("tenant-b", "user")?
        );
        assert_ne!(owner_key("ab", "c")?, owner_key("a", "bc")?);
        assert!(owner_key("a\0b", "c").is_err());
        assert_eq!(owner_key("tenant", "user")?.len(), 64);
        Ok(())
    }

    #[test]
    fn public_callback_is_loopback_even_when_a_container_binds_all_interfaces()
    -> anyhow::Result<()> {
        let local = CallbackConfig::parse("127.0.0.1:1455")?;
        assert_eq!(local.redirect, "http://127.0.0.1:1455/auth/callback");
        let container = CallbackConfig::parse("0.0.0.0:1456")?;
        assert_eq!(container.redirect, "http://127.0.0.1:1456/auth/callback");
        assert!(CallbackConfig::parse("192.0.2.1:1455").is_err());
        assert!(CallbackConfig::parse("127.0.0.1:80").is_err());
        Ok(())
    }
}
