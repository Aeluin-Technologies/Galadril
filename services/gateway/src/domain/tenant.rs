//! Canonical tenant identifier validation shared across security boundaries.

use anyhow::{Result, bail};

const MAX_TENANT_ID_LEN: usize = 64;

/// Validates the canonical tenant identifier shared across security systems.
pub fn validate_tenant_id(tenant_id: &str) -> Result<&str> {
    let tenant_id = tenant_id.trim();
    if tenant_id.is_empty() {
        bail!("tenant_id is empty");
    }
    if tenant_id.len() > MAX_TENANT_ID_LEN {
        bail!("tenant_id is too long");
    }
    if !tenant_id.bytes().all(|byte| {
        byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
    }) {
        bail!("tenant_id contains invalid characters");
    }
    Ok(tenant_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tenant_identifiers_are_normalized_and_bounded() {
        assert!(matches!(validate_tenant_id(" acme "), Ok("acme")));
        assert!(validate_tenant_id("").is_err());
        assert!(validate_tenant_id("evil;drop").is_err());
        assert!(validate_tenant_id("a/b").is_err());
        assert!(validate_tenant_id(&"a".repeat(65)).is_err());
    }
}
