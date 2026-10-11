//! Tenant-owned SSO configuration and bounded permission import contracts.

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};

/// Validates an exact email domain before ownership verification.
pub fn validate_domain(domain: &str) -> Result<()> {
    ensure!(
        domain.len() <= 253 && domain.contains('.'),
        "Invalid email domain"
    );
    ensure!(
        domain.parse::<std::net::IpAddr>().is_err(),
        "IP address is not a domain"
    );
    for label in domain.split('.') {
        ensure!(
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-'),
            "Invalid domain label"
        );
        ensure!(
            label.bytes().all(|byte| byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || byte == b'-'),
            "Domain must be canonical ASCII"
        );
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionInput {
    pub domain: String,
    pub oidc_issuer: Option<String>,
    pub oidc_client_id: Option<String>,
    pub oidc_client_secret: Option<String>,
    pub oidc_registration_token: Option<String>,
    pub saml_metadata: Option<String>,
    #[serde(default)]
    pub jit_provision: bool,
}

impl ConnectionInput {
    /// Rejects ambiguous protocols and oversized secrets before network I/O.
    pub fn validate(&self) -> Result<()> {
        validate_domain(&self.domain)?;
        match (&self.oidc_issuer, &self.saml_metadata) {
            (Some(issuer), None) => {
                ensure!(
                    issuer.starts_with("https://") && issuer.len() <= 2048,
                    "OIDC issuer requires HTTPS"
                );
                match (&self.oidc_client_id, &self.oidc_client_secret) {
                    (Some(id), Some(secret)) => ensure!(
                        !id.is_empty()
                            && id.len() <= 2048
                            && !secret.is_empty()
                            && secret.len() <= 4096
                            && self.oidc_registration_token.is_none(),
                        "Invalid OIDC credentials"
                    ),
                    (None, None) => ensure!(
                        self.oidc_registration_token
                            .as_ref()
                            .is_none_or(|token| !token.is_empty()
                                && token.len() <= 4096),
                        "Invalid registration token"
                    ),
                    _ => anyhow::bail!(
                        "Supply both OIDC credentials or use discovery"
                    ),
                }
            },
            (None, Some(metadata)) => {
                ensure!(
                    self.oidc_client_id.is_none()
                        && self.oidc_client_secret.is_none()
                        && self.oidc_registration_token.is_none(),
                    "Mixed SSO protocols"
                );
                ensure!(
                    !metadata.is_empty()
                        && metadata.len() <= 131_072
                        && !metadata.contains("<!DOCTYPE")
                        && !metadata.contains("<!ENTITY"),
                    "Invalid SAML metadata"
                );
            },
            _ => anyhow::bail!("Choose one SSO protocol"),
        }
        Ok(())
    }
}

#[derive(Serialize)]
pub struct ConnectionView {
    pub domain: String,
    pub protocol: String,
    pub enabled: bool,
    pub jit_provision: bool,
}

#[derive(Serialize)]
pub struct SsoSettings {
    pub organization_name: String,
    pub permission_import: bool,
    pub passkey_mfa: bool,
    pub connections: Vec<ConnectionView>,
}

#[derive(Serialize)]
#[serde(tag = "protocol", rename_all = "lowercase")]
pub enum ProviderDiscovery {
    Oidc {
        issuer: String,
        sign_in_url: String,
        jwks_uri: String,
        automatic_registration: bool,
    },
    Saml {
        entity_id: String,
        sign_in_url: String,
        signing_certificates: usize,
        endpoints: Vec<String>,
        saml_metadata: String,
    },
}

#[derive(Serialize)]
pub struct ProviderPreview {
    pub domain: String,
    pub domain_verified: bool,
    pub configuration: ProviderDiscovery,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionManifest {
    pub version: u8,
    pub operations: Vec<PermissionOperation>,
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum PermissionOperation {
    RoleAssignment {
        user: String,
        role: String,
        enabled: bool,
    },
    Relationship {
        resource: RelationObject,
        relation: String,
        subject: RelationObject,
        enabled: bool,
    },
    CedarPolicy {
        id: String,
        content: String,
        active: bool,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RelationObject {
    #[serde(rename = "type")]
    pub kind: String,
    pub id: String,
}

/// Prevents import identifiers from carrying an alternate tenant namespace.
pub fn validate_identifier(value: &str) -> Result<()> {
    ensure!(
        !value.is_empty()
            && value.len() <= 64
            && value.bytes().all(|byte| byte.is_ascii_alphanumeric()
                || matches!(byte, b'_' | b'-')),
        "Invalid local identifier"
    );
    Ok(())
}

impl PermissionManifest {
    /// Validates the complete batch before any administrative side effect.
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.version == 1
                && !self.operations.is_empty()
                && self.operations.len() <= 64,
            "Invalid permission manifest"
        );
        for operation in &self.operations {
            match operation {
                PermissionOperation::RoleAssignment { user, role, .. } => {
                    validate_identifier(user)?;
                    validate_identifier(role)?;
                },
                PermissionOperation::Relationship {
                    resource,
                    relation,
                    subject,
                    ..
                } => {
                    ensure!(
                        resource.kind == "group"
                            && subject.kind == "user"
                            && matches!(
                                relation.as_str(),
                                "reader" | "ingester"
                            ),
                        "Unsupported relationship"
                    );
                    validate_identifier(&resource.id)?;
                    validate_identifier(&subject.id)?;
                },
                PermissionOperation::CedarPolicy { id, content, .. } => {
                    validate_identifier(id)?;
                    ensure!(
                        !content.is_empty() && content.len() <= 8192,
                        "Invalid Cedar policy size"
                    );
                },
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn oidc_credentials_can_be_discovered_but_never_partially_supplied()
    -> Result<()> {
        let input = r#"{"domain":"acme.example","oidc_issuer":"https://idp.acme.example"}"#;
        serde_json::from_str::<ConnectionInput>(input)?.validate()?;
        for field in ["oidc_client_id", "oidc_client_secret"] {
            let partial =
                input.replace("{", &format!("{{\"{field}\":\"secret\","));
            assert!(
                serde_json::from_str::<ConnectionInput>(&partial)?
                    .validate()
                    .is_err()
            );
        }
        Ok(())
    }

    #[test]
    fn discovery_accepts_only_canonical_exact_dns_domains() -> Result<()> {
        validate_domain("acme.example")?;
        validate_domain("eu.acme.example")?;
        for domain in [
            "ACME.example",
            "localhost",
            "a..example",
            "a.example/",
            "127.0.0.1",
            "-a.example",
            "a_.example",
            "a.example\n",
        ] {
            assert!(validate_domain(domain).is_err(), "{domain}");
        }
        Ok(())
    }

    #[test]
    fn management_inputs_never_select_a_tenant_or_return_secrets() -> Result<()>
    {
        let valid = r#"{"domain":"acme.example","oidc_issuer":"https://idp.acme.example","oidc_client_id":"client","oidc_client_secret":"secret"}"#;
        serde_json::from_str::<ConnectionInput>(valid)?.validate()?;
        assert!(
            serde_json::from_str::<ConnectionInput>(
                &valid.replace("{", "{\"tenant_id\":\"other\",")
            )
            .is_err()
        );
        let view = serde_json::to_string(&SsoSettings {
            organization_name: String::new(),
            permission_import: false,
            passkey_mfa: false,
            connections: vec![],
        })?;
        assert!(!view.contains("secret"));
        Ok(())
    }

    #[test]
    fn permission_import_rejects_cross_tenant_and_unknown_operations()
    -> Result<()> {
        let valid = r#"{"version":1,"operations":[{"kind":"relationship","resource":{"type":"group","id":"research"},"relation":"reader","subject":{"type":"user","id":"alice"},"enabled":true}]}"#;
        serde_json::from_str::<PermissionManifest>(valid)?.validate()?;
        assert!(
            serde_json::from_str::<PermissionManifest>(
                &valid.replace("research", "other/research")
            )?
            .validate()
            .is_err()
        );
        assert!(
            serde_json::from_str::<PermissionManifest>(
                &valid
                    .replace("{\"version", "{\"tenant\":\"other\",\"version")
            )
            .is_err()
        );
        assert!(
            serde_json::from_str::<PermissionManifest>(
                &valid.replace("reader", "administrator")
            )?
            .validate()
            .is_err()
        );
        Ok(())
    }
}
