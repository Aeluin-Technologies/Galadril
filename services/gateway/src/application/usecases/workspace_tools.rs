//! MCP reuses live workspace IAM and compiler publication without a model
//! execution.

use galadril_document_compiler::delegation::{AuthorizationRequest, Grant};
use galadril_document_compiler::tools::JobOperation;

use super::*;
use crate::application::usecases::pengolo::{CompiledArtifact, Principal};
use crate::domain::mcp_workspace::{
    EmailDelivery, WorkspaceRequest, email_delivery, execution_id,
    required_scope,
};

#[path = "workspace_mail.rs"]
mod mail;

#[derive(PartialEq, Eq)]
pub struct WorkspaceScope {
    pub project_id: Option<String>,
}

pub struct WorkspaceIdentity {
    pub tenant: String,
    pub user: String,
    pub client: String,
    pub scopes: Vec<String>,
    pub context: QueryContext,
    pub lifetime: Duration,
}

impl ChatTools {
    pub(super) async fn authorize_conversation_mcp(
        &self,
        grant: &super::super::mcp_delegation::McpContext,
    ) -> Result<bool> {
        let Ok(scope) = self.capabilities.resolve(&grant.parent) else {
            return Ok(false);
        };
        ensure!(
            scope.workspace.is_none() &&
                !scope.pengolo &&
                grant.capability ==
                    crate::domain::mcp_workspace::CAPABILITY &&
                grant.resource == crate::domain::mcp_workspace::resource() &&
                scope.tenant_id == grant.tenant &&
                scope.user_id == grant.user &&
                scope.generation_id == grant.generation,
            "MCP parent scope changed"
        );
        self.identity
            .verify_user(&scope.tenant_id, &scope.user_id)
            .await?;
        ensure!(
            self.auth
                .is_authorized(
                    &scope.user_id,
                    &scope.tenant_id,
                    Permission::Edit,
                    "conversation",
                    &scope.conversation_id,
                    Some(&scope.context)
                )
                .await?,
            "MCP conversation unavailable"
        );
        Ok(true)
    }

    /// The MCP receives only an attenuated grant; parent authority stays
    /// inside Gateway.
    pub async fn invoke_workspace_grant(
        &self,
        token: &str,
        input: WorkspaceRequest,
    ) -> Result<Value> {
        ensure!(
            input.project_id.is_none(),
            "Generation project cannot be overridden"
        );
        let grant = self.mcp.resolve(
            token,
            crate::domain::mcp_workspace::CAPABILITY,
            &crate::domain::mcp_workspace::resource(),
        )?;
        if self.authorize_conversation_mcp(&grant).await? {
            let operation = input
                .request
                .get("operation")
                .and_then(Value::as_str)
                .context("Missing operation")?;
            ensure!(
                crate::domain::mcp_workspace::generation_operation_allowed(
                    operation, false, false
                ),
                "MCP tool outside conversation scope"
            );
            return self
                .execute(&grant.parent, serde_json::from_value(input.request)?)
                .await;
        }
        let service = self.pengolo_service()?;
        let generation = service.store.get(
            &grant.tenant,
            crate::application::ports::pengolo_store::Collection::Generation,
            &grant.generation,
            grant.temporary,
        ).await?.context("Generation unavailable")?;
        let mut context: QueryContext = serde_json::from_value(
            generation
                .data
                .get("authorization")
                .cloned()
                .context("Missing MCP authority")?,
        )?;
        context.hour_utc = i64::from(chrono::Utc::now().hour());
        let actor = Principal {
            tenant: &grant.tenant,
            user: &grant.user,
            context: &context,
        };
        service
            .authorize_mcp(
                &actor,
                &generation,
                grant.temporary,
                crate::domain::mcp_workspace::CAPABILITY,
                &grant.resource,
            )
            .await?;
        let operation = input
            .request
            .get("operation")
            .and_then(Value::as_str)
            .context("Missing operation")?;
        service
            .authorize_workspace_operation(
                &actor,
                &generation,
                grant.temporary,
                operation,
            )
            .await?;
        self.execute(&grant.parent, serde_json::from_value(input.request)?)
            .await
    }

    /// OAuth grants attenuate IAM and cannot supply tenant, actor or approval
    /// claims.
    #[tracing::instrument(skip_all)]
    pub async fn execute_workspace(
        &self,
        identity: WorkspaceIdentity,
        input: WorkspaceRequest,
    ) -> Result<Value> {
        let required = required_scope(&input.request)?;
        ensure!(
            identity.scopes.iter().any(|scope| scope == "workspace:use") &&
                crate::domain::mcp_workspace::grants_scope(
                    &identity.scopes,
                    required
                ),
            "MCP scope denied"
        );
        self.identity
            .verify_user(&identity.tenant, &identity.user)
            .await?;
        let actor = Principal {
            tenant: &identity.tenant,
            user: &identity.user,
            context: &identity.context,
        };
        ensure!(
            self.auth
                .is_authorized(
                    actor.user,
                    actor.tenant,
                    Permission::View,
                    "tenant",
                    actor.tenant,
                    Some(actor.context)
                )
                .await?,
            "MCP tenant denied"
        );
        if let Some(project) = input.project_id.as_deref() {
            self.pengolo_service()?.project(&actor, project).await?;
        }
        let audit = self
            .audit
            .begin(
                actor.tenant,
                actor.user,
                AuditTarget::new(
                    AuditAction::ScribeDatabaseQuery,
                    "mcp_client",
                    &identity.client,
                ),
                actor.context,
            )
            .await?;
        let result = self.workspace_operation(&identity, input).await;
        match result {
            Ok(value) => {
                if serde_json::to_vec(&value)?.len() > 262144 {
                    audit.failed("tool_output_limit").await?;
                    bail!("MCP output exceeds limit");
                }
                audit.succeeded().await?;
                Ok(value)
            },
            Err(error) => {
                audit.failed("mcp_tool_failed").await?;
                Err(error)
            },
        }
    }

    async fn workspace_operation(
        &self,
        identity: &WorkspaceIdentity,
        input: WorkspaceRequest,
    ) -> Result<Value> {
        let actor = Principal {
            tenant: &identity.tenant,
            user: &identity.user,
            context: &identity.context,
        };
        if input.request.get("operation").and_then(Value::as_str) ==
            Some("email_send")
        {
            return self.workspace_email_delivery(&actor, input.request).await;
        }
        if input.request.get("operation").and_then(Value::as_str) ==
            Some("email_save_draft")
        {
            return self.workspace_save_draft(identity, input.request).await;
        }
        let request: ToolRequest = serde_json::from_value(input.request)?;
        match request {
            ToolRequest::Email { request } => self
                .email
                .get()
                .context("Email tools unavailable")?
                .operation(actor.tenant, actor.user, &request, true)
                .await
                .map(crate::domain::mcp_workspace::draft_projection),
            ToolRequest::Search {
                question,
                entity_id,
                limit,
            } => {
                ensure!(
                    !question.trim().is_empty() &&
                        question.len() <= 4096 &&
                        (1..=50).contains(&limit),
                    "Invalid search bounds"
                );
                ensure!(
                    entity_id
                        .as_deref()
                        .is_none_or(|id| !id.is_empty() && id.len() <= 256),
                    "Invalid entity selector"
                );
                let query = StructuredSearchQuery {
                    text: if entity_id.is_some() {
                        None
                    } else {
                        Some(&question)
                    },
                    entity_id: entity_id.as_deref(),
                    ..StructuredSearchQuery::default()
                };
                let hits = self
                    .search
                    .structured_search(
                        actor.tenant,
                        actor.user,
                        actor.context,
                        query,
                        limit,
                    )
                    .await?;
                Ok(
                    json!({"evidence": hits.into_iter().map(evidence).collect::<Vec<_>>()}),
                )
            },
            ToolRequest::Graph { entity_id, depth } => {
                ensure!(
                    !entity_id.is_empty() &&
                        entity_id.len() <= 256 &&
                        (1..=2).contains(&depth),
                    "Invalid graph bounds"
                );
                let graph = self
                    .explore
                    .entity_relations_filtered(
                        actor.tenant,
                        actor.user,
                        actor.context,
                        &entity_id,
                        depth,
                        50,
                    )
                    .await?;
                Ok(graph_evidence(graph))
            },
            ToolRequest::Causal { entity_id } => {
                ensure!(
                    !entity_id.is_empty() && entity_id.len() <= 256,
                    "Invalid causal target"
                );
                let analysis = self
                    .causal
                    .latest(
                        actor.tenant,
                        actor.user,
                        actor.context,
                        &entity_id,
                    )
                    .await?;
                Ok(
                    json!({"analyses": analysis.into_iter().collect::<Vec<_>>()}),
                )
            },
            ToolRequest::Evidence {
                filter,
                query,
                after,
            } => {
                let request =
                    crate::application::usecases::evidence::EvidenceRequest {
                        filter,
                        query: query.as_deref(),
                        limit: 3,
                        after: after.as_deref(),
                    };
                Ok(serde_json::to_value(
                    self.search
                        .evidence(
                            actor.tenant,
                            actor.user,
                            actor.context,
                            request,
                        )
                        .await?,
                )?)
            },
            request => {
                self.workspace_document(identity, input.project_id, request)
                    .await
            },
        }
    }

    async fn workspace_document(
        &self,
        identity: &WorkspaceIdentity,
        project_id: Option<String>,
        request: ToolRequest,
    ) -> Result<Value> {
        let actor = Principal {
            tenant: &identity.tenant,
            user: &identity.user,
            context: &identity.context,
        };
        let id = execution_id(
            actor.tenant,
            actor.user,
            &identity.client,
            project_id.as_deref(),
        );
        let service = self.pengolo_service()?;
        let scope = service
            .compiler_workspace(&actor, project_id.as_deref(), &id)
            .await?;
        let compiler = self
            .compiler
            .get()
            .context("Document compiler unavailable")?;
        let token = self
            .capabilities
            .issue(Scope {
                tenant_id: identity.tenant.clone(),
                user_id: identity.user.clone(),
                conversation_id: id.clone(),
                generation_id: id,
                context: identity.context.clone(),
                expires: Instant::now() +
                    identity.lifetime.min(Duration::from_secs(900)),
                pengolo: false,
                temporary: false,
                workspace: Some(WorkspaceScope { project_id }),
            })?
            .retain_until_expiry();
        match request {
            ToolRequest::DocumentTemplates { request } => {
                service
                    .compiler_templates_execution(
                        &scope,
                        &token,
                        compiler.as_ref(),
                        request,
                    )
                    .await
            },
            ToolRequest::CompileDocument { request } => {
                compiler.compile(&token, request).await
            },
            ToolRequest::DocumentJob { request } => match request {
                JobOperation::Status { job_id } => {
                    compiler.job(&token, &job_id, false).await
                },
                JobOperation::Cancel { job_id } => {
                    compiler.job(&token, &job_id, true).await
                },
                JobOperation::Save { job_id, name } => {
                    let bytes = compiler.pdf(&token, &job_id).await?;
                    service
                        .publish_compiler_artifact(
                            &scope,
                            CompiledArtifact {
                                id: &job_id,
                                name: &name,
                                mime: "application/pdf",
                                bytes,
                            },
                        )
                        .await
                },
                JobOperation::SaveBatch { job_ids, name } => {
                    service
                        .save_compiled_batch_execution(
                            &scope,
                            &token,
                            compiler.as_ref(),
                            &job_ids,
                            &name,
                        )
                        .await
                },
            },
            _ => bail!("MCP operation unavailable"),
        }
    }

    /// Compiler callbacks recheck the live principal and project before
    /// releasing any vault bindings.
    pub async fn authorize_workspace_compiler(
        &self,
        token: &str,
        request: AuthorizationRequest,
    ) -> Result<Option<Grant>> {
        let Ok(scope) = self.capabilities.resolve(token) else {
            return Ok(None);
        };
        let Some(workspace) = &scope.workspace else {
            return Ok(None);
        };
        self.identity
            .verify_user(&scope.tenant_id, &scope.user_id)
            .await?;
        let actor = Principal {
            tenant: &scope.tenant_id,
            user: &scope.user_id,
            context: &scope.context,
        };
        let service = self.pengolo_service()?;
        let execution = service
            .compiler_workspace(
                &actor,
                workspace.project_id.as_deref(),
                &scope.generation_id,
            )
            .await?;
        Ok(Some(
            service
                .authorize_compiler_execution(&execution, request)
                .await?,
        ))
    }

    async fn workspace_email_delivery(
        &self,
        actor: &Principal<'_>,
        request: Value,
    ) -> Result<Value> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Review {
            operation: String,
            account: String,
            draft: String,
            revision: i64,
        }
        let review: Review = serde_json::from_value(request)?;
        ensure!(
            review.operation == "email_send" && review.revision > 0,
            "Invalid email review"
        );
        for id in [&review.account, &review.draft] {
            ensure!(
                id.len() == 32 &&
                    id.bytes().all(|byte| byte.is_ascii_hexdigit() &&
                        !byte.is_ascii_uppercase()),
                "Invalid email identifier"
            );
        }
        let email = self.email.get().context("Email tools unavailable")?;
        let request = json!({"operation":"draft","account":review.account,"draft":review.draft});
        let draft = email
            .operation(actor.tenant, actor.user, &request, true)
            .await?;
        let require_review = self
            .pengolo_service()?
            .requires_mcp_email_review(actor)
            .await?;
        match email_delivery(&draft, review.revision, require_review)? {
            EmailDelivery::Review => {},
            delivery => {
                if delivery == EmailDelivery::Send {
                    // OAuth scopes and client confirmation authorize only this
                    // revision. The mail service still checks ACLs and claims
                    // SMTP once.
                    for operation in ["approve", "send"] {
                        let request = json!({"operation":operation,"account":review.account,"draft":review.draft,"revision":review.revision});
                        email
                            .operation(
                                actor.tenant,
                                actor.user,
                                &request,
                                false,
                            )
                            .await?;
                    }
                    tracing::info!(event.name = "mcp.email.sent");
                }
                return Ok(
                    json!({"status":"sent","draft":review.draft,"revision":review.revision}),
                );
            },
        }
        let origin = std::env::var("MCP_DOWNLOAD_ORIGIN")
            .context("MCP public origin unavailable")?;
        let url = reqwest::Url::parse(&origin)?;
        ensure!(
            url.scheme() == "https" &&
                url.host_str().is_some() &&
                url.username().is_empty() &&
                url.password().is_none() &&
                url.query().is_none() &&
                url.fragment().is_none() &&
                matches!(url.path(), "" | "/"),
            "Invalid MCP public origin"
        );
        Ok(
            json!({"status":"approval_required", "review_url":format!("{}/mcp/email/{}/{}/{}", origin.trim_end_matches('/'), review.account, review.draft, review.revision), "message":"Review this exact draft in Galadril and confirm sending. This tool has not sent any email."}),
        )
    }
}
