//! Ephemeral least-authority delegation to existing authorized data services.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use chrono::Timelike as _;
use serde::Deserialize;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::application::ports::conversation_store::{
    ConversationStore, EvidenceSource,
};
use crate::application::usecases::audit::{
    AuditAction, AuditService, AuditTarget,
};
use crate::application::usecases::authorization::{
    Authorization, Permission, QueryContext,
};
use crate::application::usecases::explore::ExploreService;
use crate::application::usecases::identity::IdentityService;
use crate::application::usecases::search::{
    GlobalSearchHit, SearchService, StructuredSearchQuery,
};

const MAX_CAPABILITIES: usize = 256;

pub struct Scope {
    pub tenant_id: String,
    pub user_id: String,
    pub conversation_id: String,
    pub generation_id: String,
    pub context: QueryContext,
    pub expires: Instant,
}

#[derive(Default)]
pub struct Capabilities {
    scopes: Mutex<HashMap<String, Arc<Scope>>>,
}

pub struct CapabilityLease {
    token: String,
    registry: Arc<Capabilities>,
}

impl CapabilityLease {
    pub fn token(&self) -> &str {
        &self.token
    }
}

impl Drop for CapabilityLease {
    /// Revocation follows stream ownership, including early transport
    /// failures.
    fn drop(&mut self) {
        match self.registry.scopes.lock() {
            Ok(mut scopes) => {
                scopes.remove(&self.token);
            },
            Err(_) => tracing::error!(
                event.name = "chat.capability.revoke_failed",
                "Capability registry lock poisoned"
            ),
        }
    }
}

impl Capabilities {
    pub fn issue(self: &Arc<Self>, scope: Scope) -> Result<CapabilityLease> {
        let mut scopes = self
            .scopes
            .lock()
            .map_err(|_| anyhow::anyhow!("Capability registry unavailable"))?;
        scopes.retain(|_, scope| scope.expires > Instant::now());
        ensure!(scopes.len() < MAX_CAPABILITIES, "Chat capacity exceeded");
        let token =
            format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        scopes.insert(token.clone(), Arc::new(scope));
        Ok(CapabilityLease {
            token,
            registry: Arc::clone(self),
        })
    }

    pub fn resolve(&self, token: &str) -> Result<Arc<Scope>> {
        let scopes = self
            .scopes
            .lock()
            .map_err(|_| anyhow::anyhow!("Capability registry unavailable"))?;
        let scope = scopes
            .get(token)
            .filter(|scope| scope.expires > Instant::now())
            .context("Invalid chat capability")?;
        Ok(Arc::clone(scope))
    }
}

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum ToolRequest {
    Search {
        question: String,
        #[serde(default = "default_limit")]
        limit: usize,
    },
    Graph {
        entity_id: String,
        #[serde(default = "default_depth")]
        depth: u8,
    },
}

const fn default_limit() -> usize {
    10
}
const fn default_depth() -> u8 {
    1
}

pub struct ChatTools {
    pub capabilities: Arc<Capabilities>,
    identity: Arc<IdentityService>,
    auth: Arc<dyn Authorization>,
    audit: Arc<AuditService>,
    search: Arc<SearchService>,
    explore: Arc<ExploreService>,
    store: Arc<dyn ConversationStore>,
}

impl ChatTools {
    pub fn new(
        identity: Arc<IdentityService>,
        auth: Arc<dyn Authorization>,
        audit: Arc<AuditService>,
        search: Arc<SearchService>,
        explore: Arc<ExploreService>,
        store: Arc<dyn ConversationStore>,
    ) -> Self {
        Self {
            capabilities: Arc::new(Capabilities::default()),
            identity,
            auth,
            audit,
            search,
            explore,
            store,
        }
    }

    /// Binds a token to the authenticated actor rather than tool arguments.
    pub fn delegate(
        &self,
        request: &crate::application::ports::conversation_agent::AgentRequest<
            '_,
        >,
    ) -> Result<CapabilityLease> {
        self.capabilities.issue(Scope {
            tenant_id: request.tenant_id.to_owned(),
            user_id: request.user_id.to_owned(),
            conversation_id: request.conversation_id.to_owned(),
            generation_id: request.message_id.to_owned(),
            context: request.authorization.clone(),
            expires: Instant::now() + Duration::from_secs(900),
        })
    }

    /// Rechecks live access for every call, including concurrent framework
    /// tools.
    pub async fn execute(
        &self,
        token: &str,
        request: ToolRequest,
    ) -> Result<Value> {
        let scope = self.capabilities.resolve(token)?;
        let mut context = scope.context.clone();
        context.hour_utc = i64::from(chrono::Utc::now().hour());
        let operation = self
            .audit
            .begin(
                &scope.tenant_id,
                &scope.user_id,
                AuditTarget::new(
                    AuditAction::ScribeDatabaseQuery,
                    "conversation",
                    &scope.conversation_id,
                )
                .with_details(json!({"generation_id": scope.generation_id})),
                &context,
            )
            .await?;
        let result = async {
            self.identity.verify_user(&scope.tenant_id, &scope.user_id).await?;
            ensure!(self.auth.is_authorized(&scope.user_id, &scope.tenant_id, Permission::Edit, "conversation", &scope.conversation_id, Some(&context)).await?, "Authorization denied");
            match request {
                ToolRequest::Search { question, limit } => {
                    ensure!(!question.trim().is_empty() && question.len() <= 4096 && (1..=50).contains(&limit), "Invalid search bounds");
                    let hits = self.search.structured_search(&scope.tenant_id, &scope.user_id, &context, StructuredSearchQuery { text: Some(&question), ..StructuredSearchQuery::default() }, limit).await?;
                    let mut sources = Vec::with_capacity(hits.len() * 2);
                    for hit in &hits {
                        match hit {
                            GlobalSearchHit::EntityState { entity_id, state_type, state } => {
                                sources.push(source("entity_state", entity_id, Some(entity_id), None, state_type.as_deref()));
                                if let Some(event_id) = state.get("event_id").and_then(Value::as_str) { sources.push(source("event", event_id, Some(entity_id), None, state_type.as_deref())); }
                            },
                            GlobalSearchHit::Event { event_id, .. } => sources.push(source("event", event_id, None, None, None)),
                            GlobalSearchHit::Embedding { entity_id, modality, metadata, .. } => {
                                sources.push(source("entity_state", entity_id, Some(entity_id), Some(modality), None));
                                if let Some(event_id) = metadata.get("event_id").and_then(Value::as_str) { sources.push(source("event", event_id, Some(entity_id), Some(modality), None)); }
                            },
                        }
                    }
                    self.store.record_generation_evidence(&scope.tenant_id, &scope.conversation_id, &scope.generation_id, &sources).await?;
                    Ok(json!({"evidence": hits.into_iter().map(evidence).collect::<Vec<_>>() }))
                },
                ToolRequest::Graph { entity_id, depth } => {
                    ensure!(!entity_id.is_empty() && entity_id.len() <= 256 && (1..=2).contains(&depth), "Invalid graph bounds");
                    let graph = self.explore.entity_relations_filtered(&scope.tenant_id, &scope.user_id, &context, &entity_id, depth, 50).await?;
                    let sources = graph.nodes.iter().map(|node| source(
                        if node.id.starts_with("evt_") { "event" } else { "entity_state" },
                        &node.id, Some(&node.id), None, None,
                    )).collect::<Vec<_>>();
                    self.store.record_generation_evidence(&scope.tenant_id, &scope.conversation_id, &scope.generation_id, &sources).await?;
                    Ok(json!({
                        "nodes": graph.nodes.into_iter().map(|node| json!({"id": node.id, "label": node.label, "properties": node.properties})).collect::<Vec<_>>(),
                        "edges": graph.edges.into_iter().map(|edge| json!({"from_id": edge.from_id, "to_id": edge.to_id, "label": edge.label, "properties": edge.properties})).collect::<Vec<_>>()
                    }))
                },
            }
        }.await;
        match result {
            Ok(value) => {
                if serde_json::to_vec(&value)?.len() > 262144 {
                    operation.failed("tool_output_limit").await?;
                    bail!("Tool output exceeds limit");
                }
                operation.succeeded().await?;
                Ok(value)
            },
            Err(error) => {
                operation.failed("authorized_tool_failed").await?;
                Err(error)
            },
        }
    }
}

fn evidence(hit: GlobalSearchHit) -> Value {
    match hit {
        GlobalSearchHit::EntityState {
            entity_id, state, ..
        } => {
            json!({"kind": "entity_state", "entity_id": entity_id, "state": state})
        },
        GlobalSearchHit::Event {
            event_id,
            event_type,
            event_time_ms,
            properties,
        } => {
            json!({"kind": "event", "event_id": event_id, "event_type": event_type, "event_time_ms": event_time_ms, "properties": properties})
        },
        GlobalSearchHit::Embedding {
            entity_id,
            modality,
            created_at_ms,
            metadata,
            score,
        } => {
            json!({"kind": "embedding", "entity_id": entity_id, "modality": modality, "created_at_ms": created_at_ms, "metadata": metadata, "score": score})
        },
    }
}

fn source(
    resource_type: &str,
    resource_id: &str,
    entity_id: Option<&str>,
    modality: Option<&str>,
    state_type: Option<&str>,
) -> EvidenceSource {
    EvidenceSource {
        resource_type: resource_type.to_owned(),
        resource_id: resource_id.to_owned(),
        manage: false,
        entity_id: entity_id.map(str::to_owned),
        modality: modality.map(str::to_owned),
        state_type: state_type.map(str::to_owned),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use anyhow::{Result, ensure};

    use super::*;

    fn scope() -> Scope {
        Scope {
            tenant_id: "tenant_a".to_owned(),
            user_id: "user_a".to_owned(),
            conversation_id: "conversation_a".to_owned(),
            generation_id: "generation_a".to_owned(),
            context: QueryContext::default(),
            expires: Instant::now() + Duration::from_secs(60),
        }
    }

    #[test]
    fn capability_is_unforgeable_and_is_revoked_when_lease_drops() -> Result<()>
    {
        let registry = Arc::new(Capabilities::default());
        let lease = registry.issue(scope())?;
        let token = lease.token().to_owned();
        ensure!(registry.resolve("forged").is_err());
        let resolved = registry.resolve(&token)?;
        ensure!(resolved.tenant_id == "tenant_a");
        ensure!(resolved.user_id == "user_a");
        drop(lease);
        ensure!(registry.resolve(&token).is_err());
        Ok(())
    }

    #[test]
    fn expired_capability_is_denied() -> Result<()> {
        let registry = Arc::new(Capabilities::default());
        let mut expired = scope();
        expired.expires = Instant::now() - Duration::from_secs(1);
        let lease = registry.issue(expired)?;
        ensure!(registry.resolve(lease.token()).is_err());
        Ok(())
    }

    #[test]
    fn tools_reject_identity_and_arbitrary_query_arguments() {
        for value in [
            serde_json::json!({"operation": "search", "question": "q", "tenant_id": "other"}),
            serde_json::json!({"operation": "graph", "entity_id": "e", "cypher": "MATCH (n) RETURN n"}),
            serde_json::json!({"operation": "sql", "query": "SELECT * FROM secrets"}),
        ] {
            assert!(serde_json::from_value::<ToolRequest>(value).is_err());
        }
    }
}
