//! Private Polis management and authenticated public OIDC discovery.

use std::time::Duration;

use anyhow::{Context, Result, ensure};
use base64::Engine;
use reqwest::{Client, Method, Url};
use secrecy::{ExposeSecret, SecretString};
use serde_json::{Value, json};
use x509_cert::der::Decode;

use crate::application::ports::sso::SsoBroker;
use crate::domain::sso::{ConnectionInput, ProviderDiscovery};

pub struct PolisBroker {
    http: Client,
    private: Url,
    key: SecretString,
    origin: Url,
    registry: galadril_registry::grpc::RegistryClient,
}

impl PolisBroker {
    /// Disables redirects so trusted API credentials never follow another
    /// host.
    pub fn new(
        private: &str,
        key: SecretString,
        origin: &str,
        registry: galadril_registry::grpc::RegistryClient,
    ) -> Result<Self> {
        let private = Url::parse(private)?;
        ensure!(
            private.scheme() == "https" || private.scheme() == "http",
            "Invalid Polis transport"
        );
        let origin = Url::parse(origin)?;
        ensure!(
            origin.scheme() == "https"
                && origin.path() == "/"
                && origin.query().is_none()
                && origin.fragment().is_none()
                && origin.username().is_empty(),
            "Invalid public origin"
        );
        Ok(Self {
            http: Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(10))
                .connect_timeout(Duration::from_secs(3))
                .build()?,
            private,
            key,
            origin,
            registry,
        })
    }

    /// Bounds response allocation and hides upstream payloads in errors.
    async fn response_json(response: reqwest::Response) -> Result<Value> {
        Ok(serde_json::from_slice(
            &Self::response_bytes(response).await?,
        )?)
    }

    async fn response_bytes(
        mut response: reqwest::Response,
    ) -> Result<Vec<u8>> {
        ensure!(
            response.status().is_success(),
            "Identity provider request rejected"
        );
        let mut bytes = Vec::with_capacity(4096);
        while let Some(chunk) = response.chunk().await? {
            ensure!(
                bytes.len() + chunk.len() <= 262_144,
                "Identity provider response too large"
            );
            bytes.extend_from_slice(&chunk);
        }
        Ok(bytes)
    }

    fn oidc_preview(metadata: &Value) -> Result<ProviderDiscovery> {
        ensure!(
            metadata
                .get("response_types_supported")
                .and_then(Value::as_array)
                .is_some_and(|types| types
                    .iter()
                    .any(|value| value.as_str() == Some("code"))),
            "Provider does not support authorization code"
        );
        ensure!(
            metadata
                .get("id_token_signing_alg_values_supported")
                .and_then(Value::as_array)
                .is_some_and(|algorithms| algorithms.iter().any(
                    |value| matches!(value.as_str(), Some("RS256" | "ES256"))
                )),
            "Provider does not support signed identity tokens"
        );
        let field = |name| -> Result<String> {
            Ok(metadata
                .get(name)
                .and_then(Value::as_str)
                .context("Missing provider metadata")?
                .to_owned())
        };
        Ok(ProviderDiscovery::Oidc {
            issuer: field("issuer")?,
            sign_in_url: field("authorization_endpoint")?,
            jwks_uri: field("jwks_uri")?,
            automatic_registration: metadata
                .get("registration_endpoint")
                .and_then(Value::as_str)
                .is_some(),
        })
    }

    /// Parses bounded namespace-aware XML without DTDs or external entity
    /// resolution.
    fn saml_preview(metadata: &str, now: i64) -> Result<ProviderDiscovery> {
        const MD: &str = "urn:oasis:names:tc:SAML:2.0:metadata";
        const DS: &str = "http://www.w3.org/2000/09/xmldsig#";
        ensure!(
            metadata.len() <= 131_072 && !metadata.contains("<!DOCTYPE"),
            "Invalid SAML metadata"
        );
        let document = roxmltree::Document::parse_with_options(
            metadata,
            roxmltree::ParsingOptions {
                allow_dtd: false,
                nodes_limit: 4096,
                ..Default::default()
            },
        )?;
        let entity = document.root_element();
        ensure!(
            entity.has_tag_name((MD, "EntityDescriptor")),
            "Supply metadata for one SAML identity provider"
        );
        if let Some(expiry) = entity.attribute("validUntil") {
            ensure!(
                chrono::DateTime::parse_from_rfc3339(expiry)?.timestamp()
                    > now,
                "SAML metadata expired"
            );
        }
        let entity_id = entity
            .attribute("entityID")
            .context("Missing SAML entity ID")?;
        ensure!(
            !entity_id.is_empty() && entity_id.len() <= 2048,
            "Invalid SAML entity ID"
        );
        let mut providers = entity
            .children()
            .filter(|node| node.has_tag_name((MD, "IDPSSODescriptor")));
        let provider =
            providers.next().context("Missing SAML identity provider")?;
        ensure!(
            providers.next().is_none()
                && provider
                    .attribute("protocolSupportEnumeration")
                    .is_some_and(|value| value
                        .split_whitespace()
                        .any(|protocol| protocol
                            == "urn:oasis:names:tc:SAML:2.0:protocol")),
            "Ambiguous or incompatible SAML provider"
        );
        let mut signing_certificates = 0;
        for key in provider.children().filter(|node| {
            node.has_tag_name((MD, "KeyDescriptor"))
                && node.attribute("use").is_none_or(|value| value == "signing")
        }) {
            for node in key
                .descendants()
                .filter(|node| node.has_tag_name((DS, "X509Certificate")))
            {
                let encoded: String = node
                    .text()
                    .context("Missing signing certificate")?
                    .chars()
                    .filter(|ch| !ch.is_whitespace())
                    .collect();
                ensure!(
                    encoded.len() <= 16384,
                    "Signing certificate too large"
                );
                let bytes = base64::engine::general_purpose::STANDARD
                    .decode(encoded)?;
                let certificate = x509_cert::Certificate::from_der(&bytes)?;
                let validity = &certificate.tbs_certificate.validity;
                let timestamp = u64::try_from(now)?;
                ensure!(
                    validity.not_before.to_unix_duration().as_secs()
                        <= timestamp
                        && validity.not_after.to_unix_duration().as_secs()
                            > timestamp,
                    "Signing certificate is not current"
                );
                signing_certificates += 1;
            }
        }
        ensure!(
            signing_certificates > 0 && signing_certificates <= 16,
            "Missing or excessive signing certificates"
        );
        let mut sign_in_url = None;
        let mut endpoints = Vec::new();
        for endpoint in provider
            .children()
            .filter(|node| node.tag_name().namespace() == Some(MD))
            .filter_map(|node| node.attribute("Location"))
        {
            ensure!(endpoints.len() < 16, "Too many SAML endpoints");
            endpoints.push(endpoint.to_owned());
        }
        for service in provider
            .children()
            .filter(|node| node.has_tag_name((MD, "SingleSignOnService")))
        {
            if matches!(
                service.attribute("Binding"),
                Some(
                    "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
                        | "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
                )
            ) {
                let location = service
                    .attribute("Location")
                    .context("Missing SAML sign-in endpoint")?;
                let url = Url::parse(location)?;
                ensure!(
                    url.scheme() == "https"
                        && url.username().is_empty()
                        && url.password().is_none()
                        && url.fragment().is_none()
                        && url.port_or_known_default() == Some(443),
                    "Unsafe SAML endpoint"
                );
                crate::domain::sso::validate_domain(
                    url.host_str().context("Missing SAML host")?,
                )?;
                if sign_in_url.is_none() {
                    sign_in_url = Some(location.to_owned());
                }
            }
        }
        Ok(ProviderDiscovery::Saml {
            entity_id: entity_id.to_owned(),
            sign_in_url: sign_in_url
                .context("Missing compatible SAML sign-in endpoint")?,
            signing_certificates,
            endpoints,
            saml_metadata: metadata.to_owned(),
        })
    }

    async fn detect_metadata(
        &self,
        address: &str,
    ) -> Result<ProviderDiscovery> {
        let (client, url) = Self::public_client(address).await?;
        let response = client.get(url).send().await?;
        if response.status().is_success() {
            let bytes = Self::response_bytes(response).await?;
            if let Ok(metadata) = std::str::from_utf8(&bytes)
                && let Ok(preview) = Self::saml_preview(
                    metadata,
                    chrono::Utc::now().timestamp(),
                )
            {
                if let ProviderDiscovery::Saml { endpoints, .. } = &preview {
                    for endpoint in endpoints {
                        Self::public_url(endpoint).await?;
                    }
                }
                return Ok(preview);
            }
            if let Ok(metadata) = serde_json::from_slice::<Value>(&bytes)
                && let Some(issuer) =
                    metadata.get("issuer").and_then(Value::as_str)
            {
                ensure!(
                    address == issuer
                        || address
                            == format!(
                                "{}/.well-known/openid-configuration",
                                issuer.trim_end_matches('/')
                            ),
                    "OIDC discovery issuer mismatch"
                );
                return Self::oidc_preview(&self.metadata(issuer).await?);
            }
        }
        Self::oidc_preview(&self.metadata(address).await?)
    }

    /// Restricts discovery and every advertised endpoint to public HTTPS
    /// hosts.
    async fn public_url(value: &str) -> Result<Url> {
        let url = Url::parse(value)?;
        ensure!(
            url.scheme() == "https"
                && url.username().is_empty()
                && url.password().is_none()
                && url.fragment().is_none()
                && url.port_or_known_default() == Some(443),
            "OIDC endpoints require public HTTPS"
        );
        let host = url.host_str().context("Missing OIDC host")?;
        crate::domain::sso::validate_domain(host)?;
        let addresses = tokio::time::timeout(
            Duration::from_secs(3),
            tokio::net::lookup_host((host, 443)),
        )
        .await??
        .collect::<Vec<_>>();
        ensure!(
            !addresses.is_empty()
                && addresses
                    .iter()
                    .all(|address| public_address(address.ip())),
            "OIDC endpoint must resolve publicly"
        );
        Ok(url)
    }

    async fn metadata(&self, issuer: &str) -> Result<Value> {
        let issuer_url = Self::public_url(issuer).await?;
        ensure!(
            issuer_url.query().is_none(),
            "OIDC issuer cannot contain a query"
        );
        let discovery = format!(
            "{}/.well-known/openid-configuration",
            issuer.trim_end_matches('/')
        );
        let (client, url) = Self::public_client(&discovery).await?;
        let value = Self::response_json(client.get(url).send().await?).await?;
        ensure!(
            value.get("issuer").and_then(Value::as_str) == Some(issuer),
            "OIDC discovery issuer mismatch"
        );
        for field in [
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "userinfo_endpoint",
        ] {
            Self::public_url(
                value
                    .get(field)
                    .and_then(Value::as_str)
                    .context("Missing OIDC metadata endpoint")?,
            )
            .await?;
        }
        Ok(value)
    }

    /// Pins discovery DNS answers while preserving TLS hostname verification.
    async fn public_client(value: &str) -> Result<(Client, Url)> {
        let url = Self::public_url(value).await?;
        let host = url.host_str().context("Missing public host")?;
        let addresses = tokio::time::timeout(
            Duration::from_secs(3),
            tokio::net::lookup_host((host, 443)),
        )
        .await??
        .collect::<Vec<_>>();
        ensure!(
            !addresses.is_empty()
                && addresses
                    .iter()
                    .all(|address| public_address(address.ip())),
            "Endpoint changed to a private address"
        );
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(10))
            .connect_timeout(Duration::from_secs(3))
            .resolve_to_addrs(host, &addresses)
            .build()?;
        Ok((client, url))
    }

    /// Reuses encrypted credentials on updates instead of registering
    /// duplicate clients.
    async fn credentials(
        &self,
        input: &ConnectionInput,
        current: Option<&Value>,
        metadata: &Value,
    ) -> Result<(String, String)> {
        if let (Some(id), Some(secret)) =
            (&input.oidc_client_id, &input.oidc_client_secret)
        {
            return Ok((id.to_owned(), secret.to_owned()));
        }
        if let Some(provider) =
            current.and_then(|value| value.get("oidcProvider"))
        {
            return Ok((
                provider
                    .get("clientId")
                    .and_then(Value::as_str)
                    .context("Missing saved client ID")?
                    .to_owned(),
                provider
                    .get("clientSecret")
                    .and_then(Value::as_str)
                    .context("Missing saved credential")?
                    .to_owned(),
            ));
        }
        let endpoint = metadata.get("registration_endpoint").and_then(Value::as_str)
            .context("Provider does not support automatic registration; supply client credentials")?;
        let (client, url) = Self::public_client(endpoint).await?;
        let callback = self.origin.join("/api/oauth/oidc")?.to_string();
        let body = json!({"client_name":"Galadril", "redirect_uris":[callback], "grant_types":["authorization_code"],"response_types":["code"],"token_endpoint_auth_method":"client_secret_post"});
        let mut request = client.post(url).json(&body);
        if let Some(token) = &input.oidc_registration_token {
            request = request.bearer_auth(token);
        }
        let response = Self::response_json(request.send().await?).await?;
        ensure!(
            response.get("redirect_uris") == body.get("redirect_uris")
                && response
                    .get("token_endpoint_auth_method")
                    .and_then(Value::as_str)
                    == Some("client_secret_post"),
            "Provider registered incompatible client settings"
        );
        let id = response
            .get("client_id")
            .and_then(Value::as_str)
            .context("Missing registered client ID")?;
        let secret = response
            .get("client_secret")
            .and_then(Value::as_str)
            .context("Missing registered client credential")?;
        ensure!(
            !id.is_empty()
                && id.len() <= 2048
                && !secret.is_empty()
                && secret.len() <= 4096,
            "Invalid registered credentials"
        );
        Ok((id.to_owned(), secret.to_owned()))
    }
}

/// Excludes local, shared, multicast and translation ranges from discovery.
fn public_address(address: std::net::IpAddr) -> bool {
    match address {
        std::net::IpAddr::V4(ip) => {
            !ip.is_private()
                && !ip.is_loopback()
                && !ip.is_link_local()
                && !ip.is_unspecified()
                && !ip.is_broadcast()
                && !ip.is_documentation()
                && !ip.is_multicast()
                && !matches!(
                    ip.octets(),
                    [0, ..]
                        | [100, 64..=127, ..]
                        | [198, 18..=19, ..]
                        | [240..=255, ..]
                )
        },
        std::net::IpAddr::V6(ip) => {
            !ip.is_loopback()
                && !ip.is_unspecified()
                && !ip.is_multicast()
                && (ip.segments().first().copied().unwrap_or(0) & 0xe000)
                    == 0x2000
                && ip.to_ipv4_mapped().is_none()
        },
    }
}

#[async_trait::async_trait]
impl SsoBroker for PolisBroker {
    async fn detect(&self, address: &str) -> Result<ProviderDiscovery> {
        tokio::time::timeout(
            Duration::from_secs(20),
            self.detect_metadata(address),
        )
        .await?
    }

    async fn configure(
        &self,
        tenant: &str,
        input: &ConnectionInput,
        existing_id: Option<&str>,
    ) -> Result<String> {
        let mut url = self.private.join("/api/v1/sso")?;
        url.query_pairs_mut()
            .append_pair("tenant", tenant)
            .append_pair("product", "galadril");
        let existing = Self::response_json(
            self.http
                .get(url)
                .header(
                    "authorization",
                    format!("Api-Key {}", self.key.expose_secret()),
                )
                .send()
                .await?,
        )
        .await?;
        let current = existing
            .as_array()
            .context("Invalid Polis connection list")?
            .iter()
            .find(|record| {
                if let Some(id) = existing_id {
                    record.get("clientID").and_then(Value::as_str) == Some(id)
                } else {
                    record.get("name").and_then(Value::as_str)
                        == Some(input.domain.as_str())
                }
            });
        ensure!(
            existing_id.is_none() || current.is_some(),
            "Existing connection is missing; explicit identity migration required"
        );
        if let Some(current) = current {
            ensure!(
                current.get("oidcProvider").is_some()
                    == input.oidc_issuer.is_some(),
                "Protocol replacement requires an explicit identity migration"
            );
        }
        let callback = self.origin.join("/auth/polis/callback")?.to_string();
        let mut body = serde_json::Map::from_iter([
            ("tenant".to_owned(), json!(tenant)),
            ("product".to_owned(), json!("galadril")),
            ("name".to_owned(), json!(input.domain)),
            ("defaultRedirectUrl".to_owned(), json!(callback)),
            ("redirectUrl".to_owned(), json!([callback])),
        ]);
        if let Some(issuer) = &input.oidc_issuer {
            if let Some(provider) =
                current.and_then(|value| value.get("oidcProvider"))
            {
                let same_issuer = provider
                    .get("metadata")
                    .and_then(|metadata| metadata.get("issuer"))
                    .and_then(Value::as_str)
                    == Some(issuer.as_str())
                    || provider.get("discoveryUrl").and_then(Value::as_str)
                        == Some(
                            format!(
                                "{}/.well-known/openid-configuration",
                                issuer.trim_end_matches('/')
                            )
                            .as_str(),
                        );
                ensure!(
                    same_issuer,
                    "Issuer replacement requires an explicit identity migration"
                );
            }
            let metadata = self.metadata(issuer).await?;
            let (id, secret) =
                self.credentials(input, current, &metadata).await?;
            body.insert("oidcClientId".into(), json!(id));
            body.insert("oidcClientSecret".into(), json!(secret));
            body.insert("oidcMetadata".into(), metadata);
        } else {
            body.insert("rawMetadata".into(), json!(input.saml_metadata));
        }
        let method = if let Some(current) = current {
            body.insert(
                "clientID".into(),
                current
                    .get("clientID")
                    .context("Missing connection ID")?
                    .clone(),
            );
            body.insert(
                "clientSecret".into(),
                current
                    .get("clientSecret")
                    .context("Missing connection credential")?
                    .clone(),
            );
            Method::PATCH
        } else {
            Method::POST
        };
        let response = self
            .http
            .request(method, self.private.join("/api/v1/sso")?)
            .header(
                "authorization",
                format!("Api-Key {}", self.key.expose_secret()),
            )
            .header("x-polis-return-connection", "true")
            .json(&body)
            .send()
            .await?;
        let value = Self::response_json(response).await?;
        Ok(value
            .get("clientID")
            .and_then(Value::as_str)
            .context("Polis did not return a connection reference")?
            .to_owned())
    }

    async fn verify_domain(&self, domain: &str, proof: &str) -> Result<()> {
        let mut url = Url::parse("https://dns.google/resolve")?;
        url.query_pairs_mut()
            .append_pair("name", &format!("_galadril.{domain}"))
            .append_pair("type", "TXT");
        let answer =
            Self::response_json(self.http.get(url).send().await?).await?;
        let expected = format!("galadril-verification={proof}");
        ensure!(
            answer.get("Status").and_then(Value::as_u64) == Some(0)
                && answer.get("Answer").and_then(Value::as_array).is_some_and(
                    |records| records.iter().any(|record| record
                        .get("type")
                        .and_then(Value::as_u64)
                        == Some(16)
                        && record
                            .get("data")
                            .and_then(Value::as_str)
                            .is_some_and(
                                |value| value.trim_matches('"') == expected
                            ))
                ),
            "DNS domain proof is not published"
        );
        Ok(())
    }

    async fn initialize_organization(&self, tenant: &str) -> Result<()> {
        self.registry
            .clone()
            .put_tenant(galadril_registry::proto::PutTenantRequest {
                tenant_id: tenant.to_owned(),
            })
            .await
            .context("Registry organization initialization failed")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saml_preview_requires_one_idp_signing_certificates_and_safe_metadata()
    -> Result<()> {
        let metadata =
            include_str!("../../../tests/fixtures/saml-metadata.xml");
        let now =
            chrono::DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z")?
                .timestamp();
        let preview =
            serde_json::to_value(PolisBroker::saml_preview(metadata, now)?)?;
        assert_eq!(preview.get("protocol"), Some(&json!("saml")));
        assert_eq!(
            preview.get("entity_id"),
            Some(&json!("https://idp.example/entity"))
        );
        assert_eq!(
            preview.get("sign_in_url"),
            Some(&json!("https://idp.example/signin"))
        );
        for invalid in [
            metadata.replace("IDPSSODescriptor", "SPSSODescriptor"),
            metadata.replace("use=\"signing\"", "use=\"encryption\""),
            metadata.replace("https://idp.example/signin", "http://169.254.169.254/signin"),
            metadata.replace("urn:oasis:names:tc:SAML:2.0:metadata", "urn:untrusted"),
            metadata.replace("<md:EntityDescriptor", "<!DOCTYPE x [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><md:EntityDescriptor"),
            metadata.replace("entityID=", "validUntil=\"2029-01-01T00:00:00Z\" entityID="),
        ] {
            assert!(PolisBroker::saml_preview(&invalid, now).is_err());
        }
        Ok(())
    }

    #[test]
    fn oidc_preview_requires_authorization_code_and_signed_identity_tokens()
    -> Result<()> {
        let metadata = json!({"issuer":"https://idp.example", "authorization_endpoint":"https://idp.example/authorize", "token_endpoint":"https://idp.example/token", "jwks_uri":"https://idp.example/keys", "response_types_supported":["code"], "id_token_signing_alg_values_supported":["RS256"]});
        assert_eq!(
            serde_json::to_value(PolisBroker::oidc_preview(&metadata)?)?
                .get("protocol"),
            Some(&json!("oidc"))
        );
        for (field, value) in [
            ("response_types_supported", json!(["token"])),
            ("id_token_signing_alg_values_supported", json!(["none"])),
        ] {
            let mut invalid = metadata.clone();
            invalid
                .as_object_mut()
                .context("Invalid test metadata")?
                .insert(field.to_owned(), value);
            assert!(PolisBroker::oidc_preview(&invalid).is_err());
        }
        Ok(())
    }

    #[tokio::test]
    async fn existing_connections_reuse_encrypted_credentials_without_registration()
    -> Result<()> {
        let broker = PolisBroker::new(
            "http://127.0.0.1:5225",
            SecretString::from("private-api-key"),
            "https://app.example",
            galadril_registry::grpc::registry_client(
                "http://127.0.0.1:50053",
            )?,
        )?;
        let input: ConnectionInput = serde_json::from_str(
            r#"{"domain":"acme.example","oidc_issuer":"https://login.acme.example"}"#,
        )?;
        let current = json!({"oidcProvider":{"clientId":"saved-id","clientSecret":"saved-secret"}});
        assert_eq!(
            broker
                .credentials(&input, Some(&current), &json!({}))
                .await?,
            ("saved-id".into(), "saved-secret".into())
        );
        assert!(broker.credentials(&input, None, &json!({})).await.is_err());
        assert!(
            broker
                .credentials(
                    &input,
                    None,
                    &json!({"registration_endpoint":"http://169.254.169.254/"})
                )
                .await
                .is_err()
        );
        Ok(())
    }
    #[test]
    fn discovery_rejects_private_and_translation_addresses() -> Result<()> {
        for value in [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "192.0.2.1",
            "::1",
            "::ffff:127.0.0.1",
            "64:ff9b::a00:1",
            "fc00::1",
        ] {
            assert!(!public_address(value.parse()?));
        }
        assert!(public_address("1.1.1.1".parse()?));
        Ok(())
    }
}
