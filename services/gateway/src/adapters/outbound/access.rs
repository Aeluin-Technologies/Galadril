//! Fully consistent SpiceDB administration with mandatory tenant filters.

use std::time::Duration;

use anyhow::{Context, Result, ensure};
use loth::spicedb::pb::authzed::api::v1::permissions_service_client::PermissionsServiceClient;
use loth::spicedb::pb::authzed::api::v1::{self as pb};
use tonic::Request;
use tonic::metadata::MetadataValue;
use tonic::transport::{Channel, Endpoint};

use crate::application::ports::access_store::{AccessStore, AccessTuple};
use crate::application::usecases::authorization::canonical_spicedb_object_id;
use crate::domain::validate_tenant_id;

pub struct SpiceDbAccessStore {
    client: PermissionsServiceClient<Channel>,
    token: MetadataValue<tonic::metadata::Ascii>,
}

impl SpiceDbAccessStore {
    /// Reuses the authenticated internal endpoint without exposing
    /// credentials.
    pub async fn connect(endpoint: &str, token: &str) -> Result<Self> {
        let channel = Endpoint::from_shared(endpoint.to_owned())?
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .connect()
            .await
            .context("Access management is unavailable")?;
        Ok(Self {
            client: PermissionsServiceClient::new(channel),
            token: MetadataValue::try_from(format!("Bearer {token}"))?,
        })
    }

    fn request<T>(&self, value: T) -> Request<T> {
        let mut request = Request::new(value);
        request
            .metadata_mut()
            .insert("authorization", self.token.clone());
        request.set_timeout(Duration::from_secs(10));
        request
    }

    async fn read_tuples(
        &self,
        tenant_id: &str,
        limit: usize,
        optional_subject_filter: Option<pb::SubjectFilter>,
    ) -> Result<Vec<AccessTuple>> {
        let tenant = validate_tenant_id(tenant_id)?;
        let prefix = format!("{tenant}/");
        let mut client = self.client.clone();
        let response = client
            .read_relationships(self.request(pb::ReadRelationshipsRequest {
                consistency: Some(pb::Consistency {
                    requirement: Some(
                        pb::consistency::Requirement::FullyConsistent(true),
                    ),
                }),
                relationship_filter: Some(pb::RelationshipFilter {
                    optional_resource_id_prefix: prefix.clone(),
                    optional_subject_filter,
                    ..Default::default()
                }),
                optional_limit: u32::try_from(limit.clamp(1, 501))?,
                ..Default::default()
            }))
            .await
            .context("Access relationships could not be read")?;
        let mut stream = response.into_inner();
        let mut tuples = Vec::with_capacity(limit.min(501));
        while let Some(item) = stream
            .message()
            .await
            .context("Access relationships could not be read")?
        {
            let relationship =
                item.relationship.context("Invalid access relationship")?;
            let resource =
                relationship.resource.context("Missing access resource")?;
            ensure!(
                resource.object_id.starts_with(&prefix),
                "Cross-tenant access relationship rejected"
            );
            let subject =
                relationship.subject.context("Missing access subject")?;
            let object =
                subject.object.context("Missing access subject object")?;
            tuples.push(AccessTuple {
                resource_type: resource.object_type,
                resource_id: decode_id(
                    resource
                        .object_id
                        .strip_prefix(&prefix)
                        .context("Invalid resource scope")?,
                )?,
                relation: relationship.relation,
                subject_type: object.object_type,
                subject_id: if object.object_id.starts_with(&prefix) {
                    decode_id(
                        object
                            .object_id
                            .strip_prefix(&prefix)
                            .context("Invalid subject scope")?,
                    )?
                } else {
                    decode_id(&object.object_id)?
                },
                subject_relation: subject.optional_relation,
            });
        }
        Ok(tuples)
    }
}

#[async_trait::async_trait]
impl AccessStore for SpiceDbAccessStore {
    async fn tuples(
        &self,
        tenant_id: &str,
        limit: usize,
    ) -> Result<Vec<AccessTuple>> {
        self.read_tuples(tenant_id, limit, None).await
    }

    async fn tuples_for_subject(
        &self,
        tenant_id: &str,
        subject_type: &str,
        subject_id: &str,
        limit: usize,
    ) -> Result<Vec<AccessTuple>> {
        ensure!(
            matches!(subject_type, "user" | "role"),
            "Invalid access principal type"
        );
        let tenant = validate_tenant_id(tenant_id)?;
        let id = if subject_type == "user" {
            subject_id.to_owned()
        } else {
            format!("{tenant}/{subject_id}")
        };
        self.read_tuples(
            tenant,
            limit,
            Some(pb::SubjectFilter {
                subject_type: subject_type.to_owned(),
                optional_subject_id: canonical_spicedb_object_id(&id)
                    .into_owned(),
                ..Default::default()
            }),
        )
        .await
    }

    async fn set(
        &self,
        tenant_id: &str,
        tuple: &AccessTuple,
        enabled: bool,
    ) -> Result<()> {
        self.set_many(tenant_id, std::slice::from_ref(tuple), enabled)
            .await
    }

    async fn set_many(
        &self,
        tenant_id: &str,
        tuples: &[AccessTuple],
        enabled: bool,
    ) -> Result<()> {
        ensure!(
            tuples.len() <= 128,
            "Access changes must contain at most 128 relationships"
        );
        if tuples.is_empty() {
            return Ok(());
        }
        let tenant = validate_tenant_id(tenant_id)?;
        let updates = tuples
            .iter()
            .map(|tuple| {
                let resource_id = if tuple.resource_type == "tenant" {
                    tenant.to_owned()
                } else {
                    format!("{tenant}/{}", tuple.resource_id)
                };
                let subject_id = if matches!(
                    tuple.subject_type.as_str(),
                    "user" | "tenant"
                ) {
                    tuple.subject_id.clone()
                } else {
                    format!("{tenant}/{}", tuple.subject_id)
                };
                let relationship = pb::Relationship {
                    resource: Some(pb::ObjectReference {
                        object_type: tuple.resource_type.clone(),
                        object_id: canonical_spicedb_object_id(&resource_id)
                            .into_owned(),
                    }),
                    relation: tuple.relation.clone(),
                    subject: Some(pb::SubjectReference {
                        object: Some(pb::ObjectReference {
                            object_type: tuple.subject_type.clone(),
                            object_id: canonical_spicedb_object_id(
                                &subject_id,
                            )
                            .into_owned(),
                        }),
                        optional_relation: tuple.subject_relation.clone(),
                    }),
                    ..Default::default()
                };

                pb::RelationshipUpdate {
                    operation: if enabled {
                        pb::relationship_update::Operation::Touch as i32
                    } else {
                        pb::relationship_update::Operation::Delete as i32
                    },
                    relationship: Some(relationship),
                }
            })
            .collect();
        self.client
            .clone()
            .write_relationships(self.request(pb::WriteRelationshipsRequest {
                updates,
                ..Default::default()
            }))
            .await
            .context("Access relationships could not be saved")?;
        Ok(())
    }
}

/// Reverses only Gateway's canonical escaping; malformed tuples fail closed.
fn decode_id(value: &str) -> Result<String> {
    let mut bytes = Vec::with_capacity(value.len());
    let mut input = value.as_bytes().iter();
    while let Some(byte) = input.next() {
        if *byte == b'=' {
            let high = input
                .next()
                .copied()
                .and_then(|v| char::from(v).to_digit(16))
                .context("Invalid resource encoding")?;
            let low = input
                .next()
                .copied()
                .and_then(|v| char::from(v).to_digit(16))
                .context("Invalid resource encoding")?;
            bytes.push(u8::try_from((high << 4) | low)?);
        } else {
            bytes.push(*byte);
        }
    }
    String::from_utf8(bytes).context("Invalid resource identifier")
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use loth::engine::{EngineSettings, LothEngine};
    use loth::{LothConfig, SchemaMode, TextSource};
    use testcontainers_modules::testcontainers::core::{
        IntoContainerPort, WaitFor,
    };
    use testcontainers_modules::testcontainers::runners::AsyncRunner;
    use testcontainers_modules::testcontainers::{GenericImage, ImageExt};

    use super::*;
    use crate::application::usecases::authorization::GaladrilAuthContext;

    #[tokio::test]
    async fn real_spicedb_role_inheritance_grants_and_graph_reads_remain_tenant_bounded()
    -> Result<()> {
        let container = GenericImage::new("authzed/spicedb", "v1.56.2")
            .with_exposed_port(50051.tcp())
            .with_wait_for(WaitFor::message_on_stderr(
                "grpc server started serving",
            ))
            .with_cmd([
                "serve",
                "--grpc-preshared-key=test_key",
                "--datastore-engine=memory",
                "--telemetry-endpoint=",
                "--skip-release-check",
            ])
            .start()
            .await?;
        let endpoint = format!(
            "http://{}:{}",
            container.get_host().await?,
            container.get_host_port_ipv4(50051).await?
        );
        let config = LothConfig::new(endpoint.clone(), "test_key")
            .with_zed_schema(TextSource::from_inline(include_str!(
                "../../../../../schemas/spicedb/schema.zed"
            )));
        let (engine, _) = LothEngine::from_config(
            config,
            EngineSettings {
                schema_mode: SchemaMode::ApplyIfDifferent,
                enable_replication_fail_closed: false,
            },
        )
        .await?;
        let engine = Arc::new(engine);
        let access =
            SpiceDbAccessStore::connect(&endpoint, "test_key").await?;
        for tenant in ["tenant_a", "tenant_b"] {
            engine
                .register_relation("tenant", tenant, "member", "user", "alice")
                .await?;
            engine
                .register_relation(
                    "document",
                    &format!("{tenant}/report"),
                    "parent",
                    "tenant",
                    tenant,
                )
                .await?;
            for role in ["senior", "junior"] {
                engine
                    .register_relation(
                        "role",
                        &format!("{tenant}/{role}"),
                        "parent",
                        "tenant",
                        tenant,
                    )
                    .await?;
            }
        }
        engine
            .register_relation(
                "role",
                "tenant_a/senior",
                "member",
                "user",
                "alice",
            )
            .await?;
        access
            .set(
                "tenant_a",
                &AccessTuple {
                    resource_type: "role".into(),
                    resource_id: "junior".into(),
                    relation: "member".into(),
                    subject_type: "role".into(),
                    subject_id: "senior".into(),
                    subject_relation: "member".into(),
                },
                true,
            )
            .await?;
        let grant = AccessTuple {
            resource_type: "document".into(),
            resource_id: "report".into(),
            relation: "reader".into(),
            subject_type: "role".into(),
            subject_id: "junior".into(),
            subject_relation: "member".into(),
        };
        access.set("tenant_a", &grant, true).await?;
        let context = GaladrilAuthContext;
        assert!(
            engine
                .prepare_check("alice", "view", "document", "tenant_a/report")
                .fully_consistent()
                .with_context(&context)
                .check()
                .await?
        );
        assert!(
            !engine
                .prepare_check("alice", "view", "document", "tenant_b/report")
                .fully_consistent()
                .with_context(&context)
                .check()
                .await?
        );
        let tuples = access.tuples("tenant_a", 100).await?;
        assert!(tuples.iter().any(|tuple| tuple == &grant));
        let matches = access
            .tuples_for_subject("tenant_a", "role", "junior", 100)
            .await?;
        assert!(matches.iter().any(|tuple| tuple == &grant));
        assert!(matches.iter().all(|tuple| tuple.subject_type == "role" &&
            tuple.subject_id == "junior"));
        assert!(
            access
                .tuples_for_subject("tenant_b", "role", "junior", 100)
                .await?
                .is_empty()
        );
        assert!(
            tuples
                .iter()
                .all(|tuple| !tuple.resource_id.contains("tenant_b"))
        );
        assert_eq!(access.tuples("tenant_a", 1).await?.len(), 1);
        access.set("tenant_a", &grant, false).await?;
        assert!(
            !engine
                .prepare_check("alice", "view", "document", "tenant_a/report")
                .fully_consistent()
                .with_context(&context)
                .check()
                .await?
        );
        Ok(())
    }
}
