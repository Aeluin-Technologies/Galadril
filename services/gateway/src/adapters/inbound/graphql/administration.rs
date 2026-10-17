//! Fixed administrative contracts expose selectable fields instead of
//! serialized documents.

use anyhow::{Result, ensure};
use juniper::{GraphQLInputObject, GraphQLObject, GraphQLUnion};
use serde::{Deserialize, Serialize};

use super::context::AppContext;
use crate::application::ports::iam_store::CedarPolicy;
use crate::application::usecases::sso_admin::{DomainChallenge, ImportResult};
use crate::authentication::FederationConfig;
use crate::authentication::federation::FederationMapping;
use crate::domain::sso::{
    ConnectionInput, PermissionManifest, PermissionOperation,
    ProviderDiscovery, ProviderPreview, RelationObject, SsoSettings,
};

#[derive(GraphQLObject)]
#[graphql(name = "SsoConnection", context = AppContext)]
pub struct Connection {
    pub connection_id: String,
    pub domain: String,
    pub protocol: String,
    pub enabled: bool,
    pub jit_provision: bool,
}

#[derive(GraphQLObject)]
#[graphql(name = "SsoSettings", context = AppContext)]
pub struct Settings {
    pub organization_name: String,
    pub permission_import: bool,
    pub passkey_mfa: bool,
    pub connections: Vec<Connection>,
}

impl From<SsoSettings> for Settings {
    fn from(value: SsoSettings) -> Self {
        Self {
            organization_name: value.organization_name,
            permission_import: value.permission_import,
            passkey_mfa: value.passkey_mfa,
            connections: value
                .connections
                .into_iter()
                .map(|connection| Connection {
                    connection_id: connection.connection_id,
                    domain: connection.domain,
                    protocol: connection.protocol,
                    enabled: connection.enabled,
                    jit_provision: connection.jit_provision,
                })
                .collect(),
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "CedarPolicy", context = AppContext)]
pub struct Policy {
    pub id: String,
    pub content: String,
    pub active: bool,
}
impl From<CedarPolicy> for Policy {
    fn from(value: CedarPolicy) -> Self {
        Self {
            id: value.id,
            content: value.content,
            active: value.active,
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "RoleSyncMapping", context = AppContext)]
pub struct Mapping {
    pub external_group: String,
    pub role_name: String,
}
#[derive(GraphQLObject)]
#[graphql(name = "RoleSyncConfiguration", context = AppContext)]
pub struct SyncConfiguration {
    pub connection_id: String,
    pub claim: String,
    pub enabled: bool,
    pub mappings: Vec<Mapping>,
}
impl From<FederationConfig> for SyncConfiguration {
    fn from(value: FederationConfig) -> Self {
        Self {
            connection_id: value.connection_id,
            claim: value.claim,
            enabled: value.enabled,
            mappings: value
                .mappings
                .into_iter()
                .map(|mapping| Mapping {
                    external_group: mapping.external_group,
                    role_name: mapping.role_name,
                })
                .collect(),
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "DomainChallenge", context = AppContext)]
pub struct Challenge {
    pub record: String,
    pub value: String,
}
impl From<DomainChallenge> for Challenge {
    fn from(value: DomainChallenge) -> Self {
        Self {
            record: value.record,
            value: value.value,
        }
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "PermissionImportResult", context = AppContext)]
pub struct Import {
    pub applied: i32,
    pub failed_at: Option<i32>,
    pub complete: bool,
}
impl TryFrom<ImportResult> for Import {
    type Error = anyhow::Error;

    fn try_from(value: ImportResult) -> Result<Self> {
        Ok(Self {
            applied: i32::try_from(value.applied)?,
            failed_at: value.failed_at.map(i32::try_from).transpose()?,
            complete: value.complete,
        })
    }
}

#[derive(GraphQLObject)]
#[graphql(name = "OidcProvider", context = AppContext)]
pub struct OidcProvider {
    pub protocol: String,
    pub issuer: String,
    pub sign_in_url: String,
    pub jwks_uri: String,
    pub automatic_registration: bool,
}
#[derive(GraphQLObject)]
#[graphql(name = "SamlProvider", context = AppContext)]
pub struct SamlProvider {
    pub protocol: String,
    pub entity_id: String,
    pub sign_in_url: String,
    pub signing_certificates: i32,
    pub endpoints: Vec<String>,
    pub saml_metadata: String,
}
#[derive(GraphQLUnion)]
#[graphql(name = "SsoProviderConfiguration", context = AppContext)]
pub enum ProviderConfiguration {
    Oidc(OidcProvider),
    Saml(SamlProvider),
}
#[derive(GraphQLObject)]
#[graphql(name = "SsoProviderPreview", context = AppContext)]
pub struct Preview {
    pub domain: String,
    pub domain_verified: bool,
    pub configuration: ProviderConfiguration,
}
impl TryFrom<ProviderPreview> for Preview {
    type Error = anyhow::Error;

    fn try_from(value: ProviderPreview) -> Result<Self> {
        let configuration = match value.configuration {
            ProviderDiscovery::Oidc {
                issuer,
                sign_in_url,
                jwks_uri,
                automatic_registration,
            } => ProviderConfiguration::Oidc(OidcProvider {
                protocol: "oidc".into(),
                issuer,
                sign_in_url,
                jwks_uri,
                automatic_registration,
            }),
            ProviderDiscovery::Saml {
                entity_id,
                sign_in_url,
                signing_certificates,
                endpoints,
                saml_metadata,
            } => ProviderConfiguration::Saml(SamlProvider {
                protocol: "saml".into(),
                entity_id,
                sign_in_url,
                signing_certificates: i32::try_from(signing_certificates)?,
                endpoints,
                saml_metadata,
            }),
        };
        Ok(Self {
            domain: value.domain,
            domain_verified: value.domain_verified,
            configuration,
        })
    }
}

#[derive(Deserialize, GraphQLObject)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
#[graphql(name="AccessNode",context=AppContext)]
pub struct AccessNode {
    pub id: String,
    pub kind: String,
    pub label: String,
    pub resource_id: String,
}
#[derive(Deserialize, GraphQLObject)]
#[serde(deny_unknown_fields)]
#[graphql(name="AccessEdge",context=AppContext)]
pub struct AccessEdge {
    pub source: String,
    pub target: String,
    pub relation: String,
}
#[derive(Deserialize, GraphQLObject)]
#[serde(deny_unknown_fields)]
#[graphql(name="AccessGraph",context=AppContext)]
pub struct AccessGraph {
    pub nodes: Vec<AccessNode>,
    pub edges: Vec<AccessEdge>,
    pub truncated: bool,
}

#[derive(GraphQLInputObject)]
#[graphql(name = "SsoConnectionInput")]
pub struct ConnectionConfiguration {
    pub domain: String,
    pub oidc_issuer: Option<String>,
    pub oidc_client_id: Option<String>,
    pub oidc_client_secret: Option<String>,
    pub oidc_registration_token: Option<String>,
    pub saml_metadata: Option<String>,
    #[graphql(default = false)]
    pub jit_provision: bool,
}
impl From<ConnectionConfiguration> for ConnectionInput {
    fn from(value: ConnectionConfiguration) -> Self {
        Self {
            domain: value.domain,
            oidc_issuer: value.oidc_issuer,
            oidc_client_id: value.oidc_client_id,
            oidc_client_secret: value.oidc_client_secret,
            oidc_registration_token: value.oidc_registration_token,
            saml_metadata: value.saml_metadata,
            jit_provision: value.jit_provision,
        }
    }
}

#[derive(GraphQLInputObject)]
#[graphql(name = "RoleSyncMappingInput")]
pub struct MappingInput {
    pub external_group: String,
    pub role_name: String,
}
#[derive(GraphQLInputObject)]
#[graphql(name = "RoleSyncConfigurationInput")]
pub struct SyncInput {
    pub connection_id: String,
    pub claim: String,
    pub enabled: bool,
    pub mappings: Vec<MappingInput>,
}
impl From<SyncInput> for FederationConfig {
    fn from(value: SyncInput) -> Self {
        Self {
            connection_id: value.connection_id,
            claim: value.claim,
            enabled: value.enabled,
            mappings: value
                .mappings
                .into_iter()
                .map(|mapping| FederationMapping {
                    external_group: mapping.external_group,
                    role_name: mapping.role_name,
                })
                .collect(),
        }
    }
}

#[derive(GraphQLInputObject)]
#[graphql(name = "GeneralAccessInput")]
pub struct GeneralAccess {
    pub role_names: Vec<String>,
    pub start_hour_utc: Option<i32>,
    pub end_hour_utc: Option<i32>,
    pub regions: Vec<String>,
}
impl TryFrom<GeneralAccess>
    for crate::application::usecases::general_access::GeneralAccessConfig
{
    type Error = anyhow::Error;

    fn try_from(value: GeneralAccess) -> Result<Self> {
        Ok(Self {
            role_names: value.role_names,
            start_hour_utc: value
                .start_hour_utc
                .map(u8::try_from)
                .transpose()?,
            end_hour_utc: value.end_hour_utc.map(u8::try_from).transpose()?,
            regions: value.regions,
        })
    }
}

#[derive(GraphQLInputObject, Serialize)]
#[graphql(name = "PermissionSubjectInput")]
pub struct RelationInput {
    pub kind: String,
    pub id: String,
}
impl From<RelationInput> for RelationObject {
    fn from(value: RelationInput) -> Self {
        Self {
            kind: value.kind,
            id: value.id,
        }
    }
}
#[derive(GraphQLInputObject, Serialize)]
#[graphql(name = "RoleAssignmentImportInput")]
pub struct RoleAssignmentInput {
    pub user: String,
    pub role: String,
    pub enabled: bool,
}
#[derive(GraphQLInputObject, Serialize)]
#[graphql(name = "RelationshipImportInput")]
pub struct RelationshipInput {
    pub resource: RelationInput,
    pub relation: String,
    pub subject: RelationInput,
    pub enabled: bool,
}
#[derive(GraphQLInputObject, Serialize)]
#[graphql(name = "CedarPolicyImportInput")]
pub struct PolicyInput {
    pub id: String,
    pub content: String,
    pub active: bool,
}
#[derive(GraphQLInputObject, Serialize)]
#[graphql(name = "PermissionOperationInput")]
pub struct OperationInput {
    pub role_assignment: Option<RoleAssignmentInput>,
    pub relationship: Option<RelationshipInput>,
    pub cedar_policy: Option<PolicyInput>,
}
#[derive(GraphQLInputObject, Serialize)]
#[graphql(name = "PermissionManifestInput")]
pub struct ManifestInput {
    pub version: i32,
    pub operations: Vec<OperationInput>,
}
impl TryFrom<ManifestInput> for PermissionManifest {
    type Error = anyhow::Error;

    fn try_from(value: ManifestInput) -> Result<Self> {
        ensure!(
            value.operations.len() <= 64,
            "Permission manifest exceeds operation limit"
        );
        let operations = value
            .operations
            .into_iter()
            .map(|operation| {
                match (
                    operation.role_assignment,
                    operation.relationship,
                    operation.cedar_policy,
                ) {
                    (Some(value), None, None) => {
                        Ok(PermissionOperation::RoleAssignment {
                            user: value.user,
                            role: value.role,
                            enabled: value.enabled,
                        })
                    },
                    (None, Some(value), None) => {
                        Ok(PermissionOperation::Relationship {
                            resource: value.resource.into(),
                            relation: value.relation,
                            subject: value.subject.into(),
                            enabled: value.enabled,
                        })
                    },
                    (None, None, Some(value)) => {
                        Ok(PermissionOperation::CedarPolicy {
                            id: value.id,
                            content: value.content,
                            active: value.active,
                        })
                    },
                    _ => anyhow::bail!(
                        "Choose exactly one permission operation"
                    ),
                }
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(Self {
            version: u8::try_from(value.version)?,
            operations,
        })
    }
}
