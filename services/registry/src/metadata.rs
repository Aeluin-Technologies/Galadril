//! Provision only schema-instance relationships before artifact activation.

use std::time::Duration;

use anyhow::{Context, Result, ensure};
use galadril_ontology::SymbolRef;
use galadril_ontology::authorization::Instance;
use galadril_registry::gol::authorization_object_id;
use galadril_registry::gol_service::MetadataProvisioner;
use loth::spicedb::pb::authzed::api::v1::permissions_service_client::PermissionsServiceClient;
use loth::spicedb::pb::authzed::api::v1::{self as pb};
use tonic::metadata::{Ascii, MetadataValue};
use tonic::transport::{Channel, Endpoint};

pub(crate) struct SpiceDbMetadata {
    client: PermissionsServiceClient<Channel>,
    bearer: MetadataValue<Ascii>,
}

impl SpiceDbMetadata {
    /// Uses the deployment's existing authoritative SpiceDB endpoint.
    pub(crate) async fn connect(endpoint: &str, token: &str) -> Result<Self> {
        ensure!(
            !token.is_empty(),
            "metadata authority credentials are required"
        );
        let endpoint = if endpoint.starts_with("http://") ||
            endpoint.starts_with("https://")
        {
            endpoint.to_owned()
        } else {
            format!("http://{endpoint}")
        };
        let client = Endpoint::from_shared(endpoint)?
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(5))
            .connect()
            .await
            .context("metadata relationship authority unavailable")?;
        Ok(Self {
            client: PermissionsServiceClient::new(client),
            bearer: format!("Bearer {token}").parse()?,
        })
    }

    fn request<T>(&self, value: T) -> tonic::Request<T> {
        let mut request = tonic::Request::new(value);
        request
            .metadata_mut()
            .insert("authorization", self.bearer.clone());
        request.set_timeout(Duration::from_secs(5));
        request
    }
}

#[async_trait::async_trait]
impl MetadataProvisioner for SpiceDbMetadata {
    async fn provision(
        &self,
        tenant: &str,
        ontology: &str,
        author: &str,
        candidates: &[SymbolRef],
    ) -> Result<()> {
        ensure!(candidates.len() <= 4096, "schema candidate budget exceeded");
        let ontology_id =
            authorization_object_id(&format!("{tenant}/{ontology}"))
                .into_owned();
        let author = authorization_object_id(author).into_owned();
        let permission = self
            .client
            .clone()
            .check_permission(self.request(pb::CheckPermissionRequest {
                resource: Some(pb::ObjectReference {
                    object_type: "ontology".into(),
                    object_id: ontology_id.clone(),
                }),
                permission: "publish".into(),
                subject: Some(pb::SubjectReference {
                    object: Some(pb::ObjectReference {
                        object_type: "user".into(),
                        object_id: author.clone(),
                    }),
                    optional_relation: String::new(),
                }),
                consistency: Some(pb::Consistency {
                    requirement: Some(
                        pb::consistency::Requirement::FullyConsistent(true),
                    ),
                }),
                context: None,
                with_tracing: false,
            }))
            .await
            .context("publisher relationship validation unavailable")?
            .into_inner();
        ensure!(
            permission.permissionship ==
                pb::check_permission_response::Permissionship::HasPermission
                    as i32,
            "publisher permission is absent or conditional"
        );
        for candidates in candidates.chunks(128) {
            let mut updates = Vec::with_capacity(candidates.len() * 3);
            for candidate in candidates {
                let instance = Instance {
                    tenant: tenant.into(),
                    owner: candidate.clone(),
                    id: "__schema".into(),
                };
                let id = instance
                    .object_id()
                    .context("invalid schema instance identity")?;
                for (relation, subject_type, subject_id) in [
                    ("scope", "tenant", tenant),
                    ("ontology", "ontology", ontology_id.as_str()),
                    ("metadata_readers", "user", author.as_str()),
                ] {
                    updates.push(pb::RelationshipUpdate {
                        operation: pb::relationship_update::Operation::Touch
                            as i32,
                        relationship: Some(pb::Relationship {
                            resource: Some(pb::ObjectReference {
                                object_type: "gol_resource".into(),
                                object_id: id.clone(),
                            }),
                            relation: relation.into(),
                            subject: Some(pb::SubjectReference {
                                object: Some(pb::ObjectReference {
                                    object_type: subject_type.into(),
                                    object_id: subject_id.into(),
                                }),
                                optional_relation: String::new(),
                            }),
                            optional_caveat: None,
                            optional_expires_at: None,
                        }),
                    });
                }
            }
            self.client
                .clone()
                .write_relationships(self.request(
                    pb::WriteRelationshipsRequest {
                        updates,
                        optional_preconditions: Vec::new(),
                        optional_transaction_metadata: None,
                    },
                ))
                .await
                .context("schema relationship provisioning unavailable")?;
        }
        tracing::info!(
            event.name = "registry.gol.schema_provisioned",
            candidate_count = candidates.len()
        );
        Ok(())
    }
}
