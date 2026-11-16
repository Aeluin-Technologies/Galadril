//! Transport GOL obligations to SpiceDB without collapsing caveats into
//! grants.

use std::collections::BTreeSet;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use galadril_ontology::Name;
use galadril_ontology::authorization::{
    AuthorityRevision, Consistency, PermissionCheck, PermissionOutcome,
    PermissionProvider,
};
use loth::spicedb::pb::authzed::api::v1::check_permission_response::Permissionship;
use loth::spicedb::pb::authzed::api::v1::permissions_service_client::PermissionsServiceClient;
use loth::spicedb::pb::authzed::api::v1::{
    CheckPermissionRequest, CheckPermissionResponse, ObjectReference,
    SubjectReference, ZedToken, consistency,
};
use sha2::{Digest, Sha256};
use tonic::metadata::{Ascii, MetadataValue};
use tonic::transport::{Channel, Endpoint};

pub(super) struct SpiceDb {
    client: PermissionsServiceClient<Channel>,
    bearer: MetadataValue<Ascii>,
    schema: AuthorityRevision,
    bridge: AuthorityRevision,
    resource_type: Box<str>,
    permissions: BTreeSet<Name>,
}

impl SpiceDb {
    /// Uses the same deployed schema as Gateway's other authorization routes.
    pub async fn connect(
        endpoint: &str,
        token: &str,
        authority: &galadril_registry::gol::GolAuthorityConfig,
    ) -> Result<Self> {
        ensure!(!token.is_empty(), "SpiceDB credentials are required");
        let channel = Endpoint::from_shared(endpoint.to_owned())?
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(5))
            .connect()
            .await
            .context("GOL relationship authority unavailable")?;
        let bearer = format!("Bearer {token}").parse()?;
        Ok(Self {
            client: PermissionsServiceClient::new(channel),
            bearer,
            schema: authority.relationship_schema.clone(),
            bridge: authority.bridge.clone(),
            resource_type: authority.resource_type.clone().into_boxed_str(),
            permissions: authority.relationship_permissions.clone(),
        })
    }

    /// New data IDs are scoped to one immutable trusted source. Existing
    /// grants are never touched, so retrying ingestion cannot undo a
    /// revocation.
    pub async fn ensure_source_instance(
        &self,
        instance: &galadril_ontology::authorization::Instance,
        source: &str,
        original_owner: &str,
        ontology: &str,
    ) -> Result<()> {
        use loth::spicedb::pb::authzed::api::v1 as pb;
        let prefix = format!("gol:{:x}:", Sha256::digest(source.as_bytes()));
        let Some(suffix) = instance.id.strip_prefix(&prefix) else {
            // Existing shared identities require their own grants; ingestion
            // cannot assign them a new owner or source.
            return Ok(());
        };
        ensure!(
            suffix.len() == 64 &&
                suffix.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "GOL instance was not source scoped"
        );
        let raw = source
            .strip_prefix("raw:")
            .context("GOL source unavailable")?;
        ensure!(
            raw.starts_with(&format!("{}/", instance.tenant)),
            "GOL source unavailable"
        );
        let id = instance.object_id().context("GOL instance unavailable")?;
        let ontology = galadril_registry::gol::authorization_object_id(
            &format!("{}/{ontology}", instance.tenant),
        )
        .into_owned();
        let raw =
            galadril_registry::gol::authorization_object_id(raw).into_owned();
        let owner =
            galadril_registry::gol::authorization_object_id(original_owner)
                .into_owned();
        let updates = [
            ("scope", "tenant", instance.tenant.as_ref()),
            ("ontology", "ontology", ontology.as_str()),
            ("source", "raw", raw.as_str()),
            ("owner", "user", owner.as_str()),
        ]
        .into_iter()
        .map(|(relation, kind, subject)| pb::RelationshipUpdate {
            operation: pb::relationship_update::Operation::Create as i32,
            relationship: Some(pb::Relationship {
                resource: Some(pb::ObjectReference {
                    object_type: self.resource_type.to_string(),
                    object_id: id.clone(),
                }),
                relation: relation.into(),
                subject: Some(pb::SubjectReference {
                    object: Some(pb::ObjectReference {
                        object_type: kind.into(),
                        object_id: subject.into(),
                    }),
                    optional_relation: String::new(),
                }),
                optional_caveat: None,
                optional_expires_at: None,
            }),
        })
        .collect();
        let mut request = tonic::Request::new(pb::WriteRelationshipsRequest {
            updates,
            optional_preconditions: vec![pb::Precondition {
                operation: pb::precondition::Operation::MustNotMatch as i32,
                filter: Some(pb::RelationshipFilter {
                    resource_type: self.resource_type.to_string(),
                    optional_resource_id: id,
                    ..Default::default()
                }),
            }],
            optional_transaction_metadata: None,
        });
        request
            .metadata_mut()
            .insert("authorization", self.bearer.clone());
        match self.client.clone().write_relationships(request).await {
            Ok(_) => Ok(()),
            Err(status)
                if matches!(
                    status.code(),
                    tonic::Code::FailedPrecondition |
                        tonic::Code::AlreadyExists
                ) =>
            {
                Ok(())
            },
            Err(_) => anyhow::bail!("GOL instance provisioning unavailable"),
        }
    }
}

impl PermissionProvider for SpiceDb {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        if request.resource_type != self.resource_type.as_ref() ||
            request.versions.relationship_schema != self.schema ||
            request.versions.bridge != self.bridge ||
            !self.permissions.contains(request.permission) ||
            !matches!(
                request.principal.subject_type.as_ref(),
                "user" | "service"
            )
        {
            return PermissionOutcome::Unavailable;
        }
        let requirement = match request.consistency {
            Consistency::FullyConsistent => {
                consistency::Requirement::FullyConsistent(true)
            },
            Consistency::AtLeastAsFresh(token)
                if !token.is_empty() && token.len() <= 4096 =>
            {
                consistency::Requirement::AtLeastAsFresh(ZedToken {
                    token: token.to_string(),
                })
            },
            _ => return PermissionOutcome::Unavailable,
        };
        let context = (!request.context.is_empty()).then(|| loth::spicedb::pb::google::protobuf::Struct {
            fields: request.context.iter().map(|(name, value)| (name.to_string(), loth::spicedb::pb::google::protobuf::Value {kind:Some(loth::spicedb::pb::google::protobuf::value::Kind::BoolValue(*value))})).collect(),
        });
        let mut rpc = tonic::Request::new(CheckPermissionRequest {
            resource: Some(ObjectReference {
                object_type: request.resource_type.to_owned(),
                object_id: request.resource_id.to_owned(),
            }),
            permission: request.permission.to_string(),
            subject: Some(SubjectReference {
                object: Some(ObjectReference {
                    object_type: request.principal.subject_type.to_string(),
                    object_id:
                        galadril_registry::gol::authorization_object_id(
                            &request.principal.subject_id,
                        )
                        .into_owned(),
                }),
                optional_relation: String::new(),
            }),
            consistency: Some(
                loth::spicedb::pb::authzed::api::v1::Consistency {
                    requirement: Some(requirement),
                },
            ),
            context,
            with_tracing: false,
        });
        rpc.metadata_mut()
            .insert("authorization", self.bearer.clone());
        match self.client.clone().check_permission(rpc).await {
            Ok(response) => {
                outcome(response.into_inner(), request.consistency)
            },
            Err(error) => {
                tracing::warn!(
                    event.name = "gol.relationship.unavailable",
                    grpc_code = error.code() as i32
                );
                PermissionOutcome::Unavailable
            },
        }
    }
}

pub(super) fn outcome(
    response: CheckPermissionResponse,
    required: &Consistency,
) -> PermissionOutcome {
    match Permissionship::try_from(response.permissionship) {
        Ok(Permissionship::HasPermission)
            if response
                .checked_at
                .is_some_and(|token| !token.token.is_empty()) =>
        {
            PermissionOutcome::Allow(required.clone())
        },
        Ok(Permissionship::NoPermission) => PermissionOutcome::Deny,
        Ok(Permissionship::ConditionalPermission) => {
            PermissionOutcome::Conditional
        },
        _ => PermissionOutcome::Unavailable,
    }
}
