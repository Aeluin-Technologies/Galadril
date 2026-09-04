//! TerminusDB authority for tenant pipeline snapshots and ontology references.

use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use galadril_versioning::{TerminusClient, named};
use serde_json::{Value, json};

use crate::application::ports::control_plane_store::{
    ControlPlaneStore, OntologyCatalogEntry, OntologyPublication,
    PipelineExecution, PipelineOntologyBinding,
};
use crate::application::ports::pipeline_store::{
    NewPipelineRevision, PipelineDefinition, PipelineStore,
};

pub struct TerminusStore {
    client: Arc<TerminusClient>,
    executions: Arc<dyn ControlPlaneStore>,
}

impl TerminusStore {
    pub fn new(
        client: Arc<TerminusClient>,
        executions: Arc<dyn ControlPlaneStore>,
    ) -> Self {
        Self { client, executions }
    }

    fn pipeline(value: &Value, head: &str) -> Result<PipelineDefinition> {
        let mut pipeline: PipelineDefinition =
            serde_json::from_value(value.clone())?;
        pipeline.head_revision_id = head.to_owned();
        Ok(pipeline)
    }

    async fn save_pipeline(
        &self,
        tenant: &str,
        pipeline: &PipelineDefinition,
        expected: &str,
    ) -> Result<PipelineDefinition> {
        let mut document = serde_json::to_value(pipeline)?;
        document
            .as_object_mut()
            .context("Invalid pipeline document")?
            .insert(
                "@id".to_owned(),
                json!(format!("pipeline/{}", pipeline.pipeline_id)),
            );
        let head = self
            .client
            .write(
                tenant,
                &document,
                expected,
                &pipeline.author_id,
                &pipeline.message,
            )
            .await?;
        Self::pipeline(&document, &head)
    }
}

#[async_trait::async_trait]
impl PipelineStore for TerminusStore {
    async fn create(
        &self,
        tenant: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        ensure!(
            named(
                &snapshot.documents,
                &format!("pipeline/{}", revision.pipeline_id)
            )
            .is_none(),
            "Pipeline already exists"
        );
        let now = chrono::Utc::now().timestamp_millis();
        let pipeline = PipelineDefinition {
            pipeline_id: revision.pipeline_id.to_owned(),
            name: revision.name.to_owned(),
            owner_id: revision.owner_id.to_owned(),
            head_revision_id: String::new(),
            published_revision_id: None,
            definition: revision.definition.clone(),
            author_id: revision.author_id.to_owned(),
            message: revision.message.to_owned(),
            created_at_ms: now,
            updated_at_ms: now,
            deleted_at_ms: None,
        };
        self.save_pipeline(tenant, &pipeline, &snapshot.revision)
            .await
    }

    async fn update(
        &self,
        tenant: &str,
        expected: &str,
        revision: &NewPipelineRevision<'_>,
    ) -> Result<PipelineDefinition> {
        let mut current = self
            .get(tenant, revision.pipeline_id)
            .await?
            .context("Pipeline is unavailable")?;
        ensure!(
            current.head_revision_id == expected,
            "Pipeline head changed"
        );
        current.name = revision.name.to_owned();
        current.definition = revision.definition.clone();
        current.author_id = revision.author_id.to_owned();
        current.message = revision.message.to_owned();
        current.updated_at_ms = chrono::Utc::now().timestamp_millis();
        self.save_pipeline(tenant, &current, expected).await
    }

    async fn list(
        &self,
        tenant: &str,
        limit: usize,
    ) -> Result<Vec<PipelineDefinition>> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        snapshot
            .documents
            .iter()
            .filter(|doc| {
                doc.get("pipeline_id").is_some() &&
                    doc.get("deleted_at_ms").is_none_or(Value::is_null)
            })
            .take(limit.min(100))
            .map(|doc| Self::pipeline(doc, &snapshot.revision))
            .collect()
    }

    async fn get(
        &self,
        tenant: &str,
        pipeline_id: &str,
    ) -> Result<Option<PipelineDefinition>> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        named(&snapshot.documents, &format!("pipeline/{pipeline_id}"))
            .filter(|doc| doc.get("deleted_at_ms").is_none_or(Value::is_null))
            .map(|doc| Self::pipeline(doc, &snapshot.revision))
            .transpose()
    }

    async fn publish(
        &self,
        tenant: &str,
        pipeline_id: &str,
        revision: &str,
    ) -> Result<PipelineDefinition> {
        let mut current = self
            .get(tenant, pipeline_id)
            .await?
            .context("Pipeline is unavailable")?;
        ensure!(
            current.head_revision_id == revision,
            "Pipeline head changed"
        );
        current.published_revision_id = Some(revision.to_owned());
        self.save_pipeline(tenant, &current, revision).await
    }

    async fn delete(
        &self,
        tenant: &str,
        pipeline_id: &str,
        expected: &str,
    ) -> Result<()> {
        let mut current = self
            .get(tenant, pipeline_id)
            .await?
            .context("Pipeline is unavailable")?;
        ensure!(
            current.head_revision_id == expected,
            "Pipeline head changed"
        );
        current.deleted_at_ms = Some(chrono::Utc::now().timestamp_millis());
        current.published_revision_id = None;
        self.save_pipeline(tenant, &current, expected).await?;
        Ok(())
    }
}

#[async_trait::async_trait]
impl ControlPlaneStore for TerminusStore {
    async fn publish_ontology(
        &self,
        tenant: &str,
        ontology_id: &str,
        display_name: &str,
        publication_id: &str,
        revision_id: &str,
        metadata: &Value,
        create_only: bool,
    ) -> Result<OntologyPublication> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        let identity = format!("ontology/{ontology_id}");
        let existing = named(&snapshot.documents, &identity);
        ensure!(
            !create_only || existing.is_none(),
            "Ontology already exists"
        );
        ensure!(create_only || existing.is_some(), "Ontology is unavailable");
        let pinned = self.client.read(tenant, revision_id, true).await?;
        let state = named(&pinned.documents, "ontology/state")
            .context("Validated ontology revision is unavailable")?;
        let revision = state
            .get("revision")
            .context("Missing revision provenance")?;
        let materialization = state
            .get("materialization")
            .context("Missing validated materialization")?;
        ensure!(
            revision.get("tenant_id").and_then(Value::as_str) == Some(tenant) &&
                materialization.get("tenant_id").and_then(Value::as_str) ==
                    Some(tenant),
            "Ontology tenant mismatch"
        );
        let publication: OntologyPublication = serde_json::from_value(
            json!({
                "publication_id": publication_id, "revision_id": revision_id, "lifecycle": "production", "metadata": metadata,
                "base_version": revision.get("base_version"), "base_hash": revision.get("base_hash"), "effective_hash": materialization.get("effective_hash"),
                "author": revision.get("author"), "message": revision.get("message"), "published_at_ms": chrono::Utc::now().timestamp_millis(), "retired_at_ms": null
            }),
        )?;
        let document = json!({"@id": identity, "ontology_id": ontology_id, "display_name": display_name, "publication": publication});
        self.client
            .write(
                tenant,
                &document,
                &snapshot.revision,
                &publication.author,
                "Publish ontology",
            )
            .await?;
        Ok(publication)
    }

    async fn retire_ontology(
        &self,
        tenant: &str,
        ontology_id: &str,
        publication_id: &str,
        revision_id: &str,
    ) -> Result<()> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        let mut document =
            named(&snapshot.documents, &format!("ontology/{ontology_id}"))
                .context("Ontology unavailable")?
                .clone();
        let publication = document
            .get_mut("publication")
            .and_then(Value::as_object_mut)
            .context("Publication unavailable")?;
        ensure!(
            publication.get("publication_id").and_then(Value::as_str) ==
                Some(publication_id) &&
                publication.get("revision_id").and_then(Value::as_str) ==
                    Some(revision_id),
            "Ontology publication changed"
        );
        publication.insert("lifecycle".to_owned(), json!("retired"));
        publication.insert(
            "retired_at_ms".to_owned(),
            json!(chrono::Utc::now().timestamp_millis()),
        );
        self.client
            .write(
                tenant,
                &document,
                &snapshot.revision,
                "gateway",
                "Retire ontology",
            )
            .await?;
        Ok(())
    }

    async fn ontology_exists(
        &self,
        tenant: &str,
        ontology_id: &str,
    ) -> Result<bool> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        Ok(
            named(&snapshot.documents, &format!("ontology/{ontology_id}"))
                .is_some(),
        )
    }

    async fn list_ontologies(
        &self,
        tenant: &str,
        limit: usize,
    ) -> Result<Vec<OntologyCatalogEntry>> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        snapshot
            .documents
            .iter()
            .filter(|doc| doc.get("ontology_id").is_some())
            .take(limit.min(100))
            .map(|doc| {
                let publication: OntologyPublication = serde_json::from_value(
                    doc.get("publication")
                        .context("Missing publication")?
                        .clone(),
                )?;
                Ok(OntologyCatalogEntry {
                    ontology_id: doc
                        .get("ontology_id")
                        .and_then(Value::as_str)
                        .context("Missing ontology ID")?
                        .to_owned(),
                    display_name: doc
                        .get("display_name")
                        .and_then(Value::as_str)
                        .context("Missing ontology name")?
                        .to_owned(),
                    production_publication: (publication.lifecycle ==
                        "production")
                        .then_some(publication),
                })
            })
            .collect()
    }

    async fn ontology_publication_history(
        &self,
        tenant: &str,
        ontology_id: &str,
        limit: usize,
    ) -> Result<Vec<OntologyPublication>> {
        let commits = self
            .client
            .history(
                tenant,
                &format!("ontology/{ontology_id}"),
                limit.min(100),
            )
            .await?;
        let mut publications = Vec::new();
        for commit in commits {
            let snapshot = self.client.read(tenant, &commit, true).await?;
            if let Some(doc) =
                named(&snapshot.documents, &format!("ontology/{ontology_id}"))
            {
                publications.push(serde_json::from_value(
                    doc.get("publication")
                        .context("Missing publication")?
                        .clone(),
                )?);
            }
        }
        Ok(publications)
    }

    async fn list_ontology_bindings(
        &self,
        tenant: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineOntologyBinding>> {
        let snapshot = self.client.read(tenant, "main", false).await?;
        snapshot
            .documents
            .iter()
            .filter_map(|doc| doc.get("binding").map(|binding| (doc, binding)))
            .filter(|(_, binding)| {
                pipeline_id.is_none_or(|id| {
                    binding.get("pipeline_id").and_then(Value::as_str) ==
                        Some(id)
                })
            })
            .take(limit.min(100))
            .map(|(doc, binding)| {
                let selector = binding
                    .get("selector")
                    .context("Missing binding selector")?;
                Ok(PipelineOntologyBinding {
                    pipeline_id: binding
                        .get("pipeline_id")
                        .and_then(Value::as_str)
                        .context("Missing pipeline")?
                        .to_owned(),
                    block_id: binding
                        .get("block_id")
                        .and_then(Value::as_str)
                        .context("Missing block")?
                        .to_owned(),
                    ontology_id: binding
                        .get("ontology_id")
                        .and_then(Value::as_str)
                        .context("Missing ontology")?
                        .to_owned(),
                    resource_ids: selector
                        .get("resource_ids")
                        .cloned()
                        .unwrap_or(json!([])),
                    resource_kinds: selector
                        .get("kinds")
                        .cloned()
                        .unwrap_or(json!([])),
                    include_dependencies: selector
                        .get("include_dependencies")
                        .and_then(Value::as_bool)
                        .unwrap_or(true),
                    metadata: binding
                        .get("metadata")
                        .cloned()
                        .unwrap_or(json!({})),
                    updated_at_ms: doc
                        .get("updated_at_ms")
                        .and_then(Value::as_i64)
                        .context("Missing binding update timestamp")?,
                })
            })
            .collect()
    }

    async fn list_pipeline_executions(
        &self,
        tenant: &str,
        pipeline_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<PipelineExecution>> {
        self.executions
            .list_pipeline_executions(tenant, pipeline_id, limit)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn published_reference_survives_a_later_native_draft_commit() -> Result<()>
    {
        let doc = json!({"pipeline_id":"daily", "name":"Daily", "owner_id":"alice", "published_revision_id":"published", "definition":{}, "author_id":"alice", "message":"Edit", "created_at_ms":1, "updated_at_ms":2, "deleted_at_ms":null});
        let pipeline = TerminusStore::pipeline(&doc, "new-native-commit")?;
        assert_eq!(pipeline.head_revision_id, "new-native-commit");
        assert_eq!(
            pipeline.published_revision_id.as_deref(),
            Some("published")
        );
        assert!(
            serde_json::to_value(pipeline)?
                .get("head_revision_id")
                .is_none()
        );
        Ok(())
    }
}
