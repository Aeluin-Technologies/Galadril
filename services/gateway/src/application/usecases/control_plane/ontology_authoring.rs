//! Collaboration delegates semantic decisions to Registry after resource
//! authorization.

use anyhow::{Context, Result, ensure};
use galadril_registry::ontology_authoring::OntologyCommand;
use serde_json::Value;

use super::{ControlPlaneService, Permission, QueryContext, bounded};
use crate::application::ports::control_plane_store::OntologyWrite;
use crate::application::usecases::audit::{AuditAction, AuditTarget};

struct OntologyMutation<'a> {
    tenant: &'a str,
    user: &'a str,
    context: &'a QueryContext,
    id: &'a str,
    expected: &'a str,
    name: &'a str,
    definition: &'a Value,
    command: &'a OntologyCommand,
}

impl ControlPlaneService {
    /// Metadata commands cannot bypass the typed native and plugin draft
    /// operations.
    pub async fn author_ontology(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        expected: &str,
        command: OntologyCommand,
    ) -> Result<Value> {
        ensure!(
            !matches!(&command, OntologyCommand::SaveDraft { .. }),
            "use a typed ontology draft operation"
        );
        self.mutate_ontology(OntologyMutation {
            tenant,
            user,
            context,
            id,
            expected,
            name: "",
            definition: &Value::Null,
            command: &command,
        })
        .await
    }

    /// Branch drafts use the same explicit permission and provenance boundary
    /// as main.
    #[expect(
        clippy::too_many_arguments,
        reason = "Keeps actor, model, content and revision explicit at the API boundary"
    )]
    pub async fn save_ontology_branch(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        name: &str,
        definition: &Value,
        expected: &str,
        branch: &str,
    ) -> Result<String> {
        ensure!(
            galadril_registry::authoring::valid_branch(branch),
            "invalid ontology branch"
        );
        ensure!(
            !name.trim().is_empty() && name.len() <= 256,
            "invalid ontology name"
        );
        galadril_registry::semantics::validate_ontology(definition)?;
        let command = OntologyCommand::SaveDraft {
            branch: branch.to_owned(),
        };
        let result = self
            .mutate_ontology(OntologyMutation {
                tenant,
                user,
                context,
                id,
                expected,
                name,
                definition,
                command: &command,
            })
            .await?;
        result
            .get("revision_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .context("ontology revision unavailable")
    }

    async fn mutate_ontology(
        &self,
        input: OntologyMutation<'_>,
    ) -> Result<Value> {
        Self::validate_ontology_id(input.id)?;
        Self::validate_ontology_revision_id(input.expected)?;
        let permission = if matches!(input.command, OntologyCommand::Delete) {
            Permission::Delete
        } else {
            Permission::Edit
        };
        ensure!(
            self.ontology_permission(
                input.tenant,
                input.user,
                input.context,
                Some(input.id),
                permission
            )
            .await?,
            "ontology authoring access denied"
        );
        let action = match input.command {
            OntologyCommand::Delete => AuditAction::DeleteOntology,
            OntologyCommand::Restore => AuditAction::RestoreOntology,
            _ => AuditAction::AuthorOntology,
        };
        let audit = self
            .audit
            .begin(
                input.tenant,
                input.user,
                AuditTarget::new(action, "ontology", input.id)
                    .with_revision_id(input.expected),
                input.context,
            )
            .await?;
        let result = self
            .store
            .author_ontology(OntologyWrite {
                tenant: input.tenant,
                ontology: input.id,
                user: input.user,
                expected: input.expected,
                name: input.name,
                definition: input.definition,
                command: input.command,
            })
            .await;
        match result {
            Ok(value) => {
                audit.succeeded().await?;
                Ok(value)
            },
            Err(error) => {
                audit.failed("ontology_authoring_failed").await?;
                Err(error)
            },
        }
    }

    /// Historical workspace inspection uses the same per-model authorization
    /// as the live editor.
    pub async fn ontology_revision(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        revision: &str,
    ) -> Result<Value> {
        self.verify_user(tenant, user).await?;
        Self::validate_ontology_id(id)?;
        Self::validate_ontology_revision_id(revision)?;
        ensure!(
            self.can_view(tenant, user, context, "ontology", id).await?,
            "ontology access denied"
        );
        self.store.ontology_workspace(tenant, id, revision).await
    }

    /// History remains ontology-scoped even when a caller knows another
    /// revision ID.
    pub async fn ontology_history(
        &self,
        tenant: &str,
        user: &str,
        context: &QueryContext,
        id: &str,
        after: Option<&str>,
        limit: usize,
    ) -> Result<Value> {
        self.verify_user(tenant, user).await?;
        Self::validate_ontology_id(id)?;
        if let Some(revision) = after {
            Self::validate_ontology_revision_id(revision)?;
        }
        ensure!(
            self.can_view(tenant, user, context, "ontology", id).await?,
            "ontology access denied"
        );
        self.store
            .ontology_history(tenant, id, after, bounded(limit))
            .await
    }
}
