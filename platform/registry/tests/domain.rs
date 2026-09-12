//! Verifies immutable publication and tenant isolation at the Registry
//! boundary.

use std::collections::BTreeMap;
use std::sync::Mutex;

use anyhow::{Context, Result, ensure};
use async_trait::async_trait;
use galadril_registry::domain::{PutOntology, PutPipeline, Registry};
use galadril_registry::state::OntologyBindingRecord;
use galadril_registry::storage::{RevisionStore, Snapshot, TenantSnapshot};
use serde_json::json;

#[derive(Default)]
struct MemoryStore {
    tenants: Mutex<BTreeMap<String, Vec<StoredSnapshot>>>,
}

struct StoredSnapshot {
    revision_id: String,
    state_json: Vec<u8>,
}

#[async_trait]
impl RevisionStore for MemoryStore {
    async fn tenant_exists(&self, tenant: &str) -> Result<bool> {
        let tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        Ok(tenants.contains_key(tenant))
    }

    async fn ensure_tenant(&self, tenant: &str) -> Result<TenantSnapshot> {
        let mut tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        let snapshots = tenants.entry(tenant.to_owned()).or_default();
        Ok(TenantSnapshot {
            revision_id: snapshots.last().map_or_else(
                || "00000000000000000000".to_owned(),
                |snapshot| snapshot.revision_id.clone(),
            ),
        })
    }

    async fn delete_tenant(&self, tenant: &str) -> Result<()> {
        let mut tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        tenants.remove(tenant).context("tenant unavailable")?;
        Ok(())
    }

    async fn load(
        &self,
        tenant: &str,
        revision: Option<&str>,
    ) -> Result<Snapshot> {
        let tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        let snapshots = tenants.get(tenant).context("tenant unavailable")?;
        let stored = if let Some(revision) = revision {
            Some(
                snapshots
                    .iter()
                    .find(|item| item.revision_id == revision)
                    .context("revision unavailable")?,
            )
        } else {
            snapshots.last()
        };
        let Some(stored) = stored else {
            return Ok(Snapshot {
                revision_id: "00000000000000000000".to_owned(),
                state: Default::default(),
            });
        };
        Ok(Snapshot {
            revision_id: stored.revision_id.clone(),
            state: serde_json::from_slice(&stored.state_json)?,
        })
    }

    async fn commit(
        &self,
        tenant: &str,
        expected: &str,
        state: &galadril_registry::state::TenantState,
        _author: &str,
        _message: &str,
    ) -> Result<String> {
        let mut tenants =
            self.tenants.lock().map_err(|_| anyhow::anyhow!("lock"))?;
        let snapshots = tenants.entry(tenant.to_owned()).or_default();
        let current = snapshots
            .last()
            .map_or("00000000000000000000", |item| item.revision_id.as_str());
        ensure!(current == expected, "registry revision changed");
        let revision = format!("{:020}", snapshots.len() + 1);
        snapshots.push(StoredSnapshot {
            revision_id: revision.clone(),
            state_json: serde_json::to_vec(state)?,
        });
        Ok(revision)
    }
}

fn definition(name: &str) -> serde_json::Value {
    json!({
        "name": name,
        "sources": [{"id":"source"}],
        "pipeline": [{"step":"sink","type":"sink","input_from":["source"]}]
    })
}

fn ontology(description: &str) -> serde_json::Value {
    json!({
        "version": "1",
        "resources": [
            {
                "resource_id":"object.person",
                "kind":"object_type",
                "display_name":"Person",
                "description":description,
                "references":[],
                "attributes":{}
            },
            {
                "resource_id":"property.person.name",
                "kind":"property",
                "display_name":"name",
                "owner_id":"object.person",
                "value_type":"string",
                "references":[],
                "attributes":{}
            }
        ]
    })
}

#[tokio::test]
async fn publication_pins_an_immutable_pipeline_revision_per_tenant()
-> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let first = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v1"),
            author: "author",
            message: "create",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    registry
        .publish_pipeline("tenant_a", "daily", &first.head_revision_id)
        .await?;
    let current = registry.current_pipeline("tenant_a", "daily").await?;
    let updated = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v2"),
            author: "author",
            message: "update",
            expected_revision_id: Some(&current.head_revision_id),
            create_only: false,
        })
        .await?;

    let published = registry
        .runtime_pipeline("tenant_a", "daily", &first.head_revision_id)
        .await?;
    assert_eq!(published.definition.get("name"), Some(&json!("v1")));
    assert_eq!(updated.record.definition.get("name"), Some(&json!("v2")));
    assert!(
        registry
            .runtime_pipeline("tenant_b", "daily", &first.head_revision_id)
            .await
            .is_err()
    );
    Ok(())
}

#[tokio::test]
async fn stale_pipeline_update_fails_closed() -> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let first = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v1"),
            author: "author",
            message: "create",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    let stale = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("v2"),
            author: "author",
            message: "update",
            expected_revision_id: Some("99999999999999999999"),
            create_only: false,
        })
        .await;
    assert!(stale.is_err());
    assert_eq!(
        registry
            .current_pipeline("tenant_a", "daily")
            .await?
            .head_revision_id,
        first.head_revision_id
    );
    Ok(())
}

#[tokio::test]
async fn ontology_binding_and_slice_remain_pinned_to_pipeline_revision()
-> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let ontology_revision = registry
        .put_ontology(PutOntology {
            tenant_id: "tenant_a",
            ontology_id: "people",
            display_name: "People",
            ontology: ontology("published"),
            author: "author",
            message: "create ontology",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    registry
        .publish_ontology(
            "tenant_a",
            "people",
            "People",
            "publication-1",
            &ontology_revision.revision_id,
            json!({"channel":"stable"}),
            false,
        )
        .await?;
    let head = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("daily"),
            author: "author",
            message: "create pipeline",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    let (binding, binding_revision) = registry
        .put_binding(
            "tenant_a",
            &head.head_revision_id,
            OntologyBindingRecord {
                pipeline_id: "daily".to_owned(),
                block_id: "sink".to_owned(),
                ontology_id: "people".to_owned(),
                resource_ids: vec!["property.person.name".to_owned()],
                resource_kinds: Vec::new(),
                include_dependencies: true,
                metadata: json!({"purpose":"identity"}),
                updated_at_ms: 42,
            },
        )
        .await?;
    assert_eq!(binding.block_id, "sink");
    registry
        .publish_pipeline("tenant_a", "daily", &binding_revision)
        .await?;

    let slice = registry
        .ontology_slice("tenant_a", "daily", &binding_revision, "sink")
        .await?;
    let resources = slice
        .ontology
        .get("resources")
        .and_then(serde_json::Value::as_array)
        .context("slice resources missing")?;
    assert_eq!(resources.len(), 2);
    assert_eq!(slice.publication.revision_id, ontology_revision.revision_id);
    assert_eq!(slice.binding.metadata, json!({"purpose":"identity"}));

    let bindings = registry
        .list_bindings("tenant_a", Some("daily"), Some(&binding_revision), 10)
        .await?;
    assert_eq!(bindings.len(), 1);
    let ontologies = registry.list_ontologies("tenant_a", 10).await?;
    assert_eq!(ontologies.len(), 1);
    Ok(())
}

#[tokio::test]
async fn delete_and_retire_remove_active_pointers_without_history_api()
-> Result<()> {
    let registry = Registry::new(MemoryStore::default());
    let ontology_revision = registry
        .put_ontology(PutOntology {
            tenant_id: "tenant_a",
            ontology_id: "people",
            display_name: "People",
            ontology: ontology("active"),
            author: "author",
            message: "create ontology",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    registry
        .publish_ontology(
            "tenant_a",
            "people",
            "People",
            "publication-1",
            &ontology_revision.revision_id,
            json!({}),
            false,
        )
        .await?;
    let pipeline = registry
        .put_pipeline(PutPipeline {
            tenant_id: "tenant_a",
            pipeline_id: "daily",
            name: "Daily",
            owner_id: "owner",
            definition: definition("daily"),
            author: "author",
            message: "create pipeline",
            expected_revision_id: None,
            create_only: true,
        })
        .await?;
    let deleted_revision = registry
        .delete_pipeline("tenant_a", "daily", &pipeline.head_revision_id)
        .await?;
    assert!(
        registry
            .current_pipeline("tenant_a", "daily")
            .await
            .is_err()
    );
    assert!(registry.list_pipelines("tenant_a", 10).await?.is_empty());
    let retired_revision = registry
        .retire_ontology(
            "tenant_a",
            "people",
            "publication-1",
            &ontology_revision.revision_id,
        )
        .await?;
    assert_ne!(deleted_revision, retired_revision);
    let catalog = registry.list_ontologies("tenant_a", 10).await?;
    let lifecycle = catalog
        .first()
        .and_then(|item| item.production_publication.as_ref())
        .map(|publication| publication.lifecycle.as_str());
    assert_eq!(lifecycle, Some("retired"));
    Ok(())
}
