//! Transport GOL obligations to SpiceDB without collapsing caveats into
//! grants.

use std::collections::{BTreeSet, HashMap};
use std::future::Future;
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
use loth::types::AuthError;
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;
use tonic::metadata::{Ascii, MetadataValue};
use tonic::transport::{Channel, Endpoint};

use super::relationship_retry::resilient_structural_check;

const RPC_TIMEOUT: Duration = Duration::from_secs(5);

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
            .connect_timeout(RPC_TIMEOUT)
            .timeout(RPC_TIMEOUT)
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

impl SpiceDb {
    pub fn live(&self) -> DecisionProvider<'_> {
        DecisionProvider {
            authority: self,
            consistency: Consistency::FullyConsistent,
            snapshot: None,
        }
    }

    /// A fully consistent check selects the revision. Its authorization result
    /// cannot grant access to any record in the subsequent decision.
    pub async fn read_snapshot(
        &self,
        user: &str,
    ) -> Option<DecisionProvider<'_>> {
        let response = self
            .check_rpc(CheckPermissionRequest {
                resource: Some(ObjectReference {
                    object_type: self.resource_type.to_string(),
                    object_id: "__gol_snapshot".into(),
                }),
                subject: Some(SubjectReference {
                    object: Some(ObjectReference {
                        object_type: "user".into(),
                        object_id:
                            galadril_registry::gol::authorization_object_id(
                                user,
                            )
                            .into_owned(),
                    }),
                    optional_relation: String::new(),
                }),
                permission: self.permissions.first()?.to_string(),
                consistency: Some(wire_consistency(
                    consistency::Requirement::FullyConsistent(true),
                )),
                ..Default::default()
            })
            .await?;
        let consistency = snapshot_consistency(response)?;
        Some(DecisionProvider {
            authority: self,
            consistency,
            snapshot: Some(ReadSnapshot::default()),
        })
    }

    fn request(
        &self,
        request: &PermissionCheck<'_>,
    ) -> Option<CheckPermissionRequest> {
        if request.resource_type != self.resource_type.as_ref() ||
            request.versions.relationship_schema != self.schema ||
            request.versions.bridge != self.bridge ||
            !self.permissions.contains(request.permission) ||
            !matches!(
                request.principal.subject_type.as_ref(),
                "user" | "service"
            )
        {
            return None;
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
            Consistency::AtExactSnapshot(token)
                if !token.is_empty() && token.len() <= 4096 =>
            {
                consistency::Requirement::AtExactSnapshot(ZedToken {
                    token: token.to_string(),
                })
            },
            _ => return None,
        };
        let context = (!request.context.is_empty()).then(|| loth::spicedb::pb::google::protobuf::Struct {
            fields: request.context.iter().map(|(name, value)| (name.to_string(), loth::spicedb::pb::google::protobuf::Value {kind:Some(loth::spicedb::pb::google::protobuf::value::Kind::BoolValue(*value))})).collect(),
        });
        Some(CheckPermissionRequest {
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
            consistency: Some(wire_consistency(requirement)),
            context,
            with_tracing: false,
        })
    }

    async fn check_rpc(
        &self,
        rpc: CheckPermissionRequest,
    ) -> Option<CheckPermissionResponse> {
        // A retry reads the same live policy without extending the RPC budget.
        let result = tokio::time::timeout(
            RPC_TIMEOUT,
            resilient_structural_check(|| async {
                let mut attempt = tonic::Request::new(rpc.clone());
                attempt
                    .metadata_mut()
                    .insert("authorization", self.bearer.clone());
                self.client.clone().check_permission(attempt).await.map_err(
                    |status| AuthError::SpiceDbStatus {
                        operation: "check_permission",
                        status,
                    },
                )
            }),
        )
        .await;
        let code = match result {
            Ok(Ok(response)) => return Some(response.into_inner()),
            Ok(Err(AuthError::SpiceDbStatus { status, .. })) => status.code(),
            Ok(Err(_)) => tonic::Code::Unknown,
            Err(_) => tonic::Code::DeadlineExceeded,
        };
        tracing::warn!(
            event.name = "gol.relationship.unavailable",
            grpc_code = code as i32
        );
        None
    }
}

impl PermissionProvider for SpiceDb {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        let Some(rpc) = self.request(&request) else {
            return PermissionOutcome::Unavailable;
        };
        self.check_rpc(rpc)
            .await
            .map_or(PermissionOutcome::Unavailable, |response| {
                outcome(response, request.consistency)
            })
    }
}

fn wire_consistency(
    requirement: consistency::Requirement,
) -> loth::spicedb::pb::authzed::api::v1::Consistency {
    loth::spicedb::pb::authzed::api::v1::Consistency {
        requirement: Some(requirement),
    }
}

pub(super) fn snapshot_consistency(
    response: CheckPermissionResponse,
) -> Option<Consistency> {
    let permission = Permissionship::try_from(response.permissionship).ok()?;
    (permission != Permissionship::Unspecified).then_some(())?;
    let token = response.checked_at?.token;
    (!token.is_empty() && token.len() <= 4096)
        .then(|| Consistency::AtExactSnapshot(token.into()))
}

/// Reuses exact relationship results only within one read decision.
pub(super) struct DecisionProvider<'a> {
    authority: &'a SpiceDb,
    pub consistency: Consistency,
    snapshot: Option<ReadSnapshot>,
}

impl DecisionProvider<'_> {
    pub fn is_pinned(&self) -> bool {
        self.snapshot.is_some()
    }
}

impl PermissionProvider for DecisionProvider<'_> {
    async fn check(&self, request: PermissionCheck<'_>) -> PermissionOutcome {
        let Some(snapshot) = &self.snapshot else {
            return self.authority.check(request).await;
        };
        if request.consistency != &self.consistency {
            return PermissionOutcome::Unavailable;
        }
        let Some(rpc) = self.authority.request(&request) else {
            return PermissionOutcome::Unavailable;
        };
        snapshot
            .check(rpc, request.consistency, |rpc| {
                self.authority.check_rpc(rpc)
            })
            .await
    }
}

#[derive(Default)]
pub(super) struct ReadSnapshot {
    state: Mutex<ReadState>,
}

#[derive(Default)]
struct ReadState {
    checks: HashMap<String, Vec<(CheckPermissionRequest, Permissionship)>>,
    calls: usize,
    reused: usize,
    retained: usize,
}

impl Drop for ReadSnapshot {
    fn drop(&mut self) {
        let state = self.state.get_mut();
        if state.calls != 0 {
            tracing::debug!(
                event.name = "gol.relationship.read_snapshot",
                checks = state.calls,
                reused = state.reused
            );
        }
    }
}

impl ReadSnapshot {
    pub async fn check<F: Future<Output = Option<CheckPermissionResponse>>>(
        &self,
        rpc: CheckPermissionRequest,
        required: &Consistency,
        call: impl FnOnce(CheckPermissionRequest) -> F,
    ) -> PermissionOutcome {
        let Consistency::AtExactSnapshot(revision) = required else {
            return PermissionOutcome::Unavailable;
        };
        if !matches!(
            rpc.consistency.as_ref().and_then(|wire| wire.requirement.as_ref()),
            Some(consistency::Requirement::AtExactSnapshot(token))
                if token.token == revision.as_ref()
        ) {
            return PermissionOutcome::Unavailable;
        }
        let mut state = self.state.lock().await;
        let Some(resource) = rpc.resource.as_ref() else {
            return PermissionOutcome::Unavailable;
        };
        if let Some(permission) =
            state.checks.get(&resource.object_id).and_then(|checks| {
                checks.iter().find_map(|(prior, decision)| {
                    (prior == &rpc).then_some(*decision)
                })
            })
        {
            state.reused += 1;
            return known_outcome(permission, required);
        }
        state.calls += 1;
        let Some(response) = call(rpc.clone()).await else {
            return PermissionOutcome::Unavailable;
        };
        if response
            .checked_at
            .as_ref()
            .is_none_or(|token| token.token != revision.as_ref())
        {
            return PermissionOutcome::Unavailable;
        }
        let Ok(permission) = Permissionship::try_from(response.permissionship)
        else {
            return PermissionOutcome::Unavailable;
        };
        if permission != Permissionship::Unspecified && state.retained < 4096 {
            state
                .checks
                .entry(resource.object_id.clone())
                .or_default()
                .push((rpc, permission));
            state.retained += 1;
        }
        known_outcome(permission, required)
    }
}

fn known_outcome(
    permission: Permissionship,
    required: &Consistency,
) -> PermissionOutcome {
    match permission {
        Permissionship::HasPermission => {
            PermissionOutcome::Allow(required.clone())
        },
        Permissionship::NoPermission => PermissionOutcome::Deny,
        Permissionship::ConditionalPermission => {
            PermissionOutcome::Conditional
        },
        Permissionship::Unspecified => PermissionOutcome::Unavailable,
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
