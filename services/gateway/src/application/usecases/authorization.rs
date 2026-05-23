//! FGAC use cases using Cedar and AGE graphs with TTL caching.

use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use cedar_policy::{
    Authorizer, Context as CedarContext, Entities, Entity, EntityId,
    EntityTypeName, EntityUid, PolicyId, PolicySet, Request,
};
use serde_json::json;
use tokio::sync::RwLock;

use crate::application::ports::entity_provider::EntityGraphProvider;
use crate::application::ports::policy_store::PolicyStore;

/// Dynamic context representing fine-grained request parameters.
#[derive(Debug, Default, Clone)]
pub struct QueryContext {
    pub entity_id: Option<String>,
    pub modality: Option<String>,
    pub state_type: Option<String>,
    pub gis_zone: Option<String>,
}

/// Action types mapped to Cedar actions.
pub enum Action {
    ReadTable,
    DiscoverTables,
}

impl Action {
    fn action_uid(&self) -> Result<EntityUid> {
        match self {
            Action::ReadTable => {
                EntityUid::from_str("Action::\"Read\"").map_err(Into::into)
            },
            Action::DiscoverTables => {
                EntityUid::from_str("Action::\"Discover\"").map_err(Into::into)
            },
        }
    }
}

/// Internal cache structure for a single tenant's Cedar environment.
struct TenantAuthCache {
    policy_set: Arc<PolicySet>,
    entities: Arc<Entities>,
    expires_at: Instant,
}

/// Service responsible for evaluating access control with optimized caching.
pub struct AuthService {
    policy_store: Arc<dyn PolicyStore>,
    entity_provider: Arc<dyn EntityGraphProvider>,
    cache_by_tenant: RwLock<HashMap<String, TenantAuthCache>>,
    cache_ttl: Duration,
}

impl AuthService {
    /// Creates a new [`AuthService`] with a specified cache TTL.
    pub fn new(
        policy_store: Arc<dyn PolicyStore>,
        entity_provider: Arc<dyn EntityGraphProvider>,
        cache_ttl: Duration,
    ) -> Self {
        Self {
            policy_store,
            entity_provider,
            cache_by_tenant: RwLock::new(HashMap::new()),
            cache_ttl,
        }
    }

    pub async fn invalidate_tenant_cache(&self, tenant_id: &str) {
        let mut cache_guard = self.cache_by_tenant.write().await;
        cache_guard.remove(tenant_id);
    }

    /// Loads the Cedar environment from cache or database if expired.
    async fn get_cedar_environment(
        &self,
        tenant_id: &str,
    ) -> Result<(Arc<PolicySet>, Arc<Entities>)> {
        {
            let cache_guard = self.cache_by_tenant.read().await;
            if let Some(cache) = cache_guard.get(tenant_id) &&
                cache.expires_at > Instant::now()
            {
                return Ok((
                    Arc::clone(&cache.policy_set),
                    Arc::clone(&cache.entities),
                ));
            }
        }

        let records = self.policy_store.get_active_policies(tenant_id).await?;
        let mut policy_set = PolicySet::new();

        for record in records {
            let policy = cedar_policy::Policy::parse(
                Some(PolicyId::new(&record.id)),
                &record.content,
            )
            .with_context(|| {
                format!("Failed to parse Cedar policy ID: {}", record.id)
            })?;
            policy_set.add(policy).with_context(|| {
                format!("Failed to add policy ID: {}", record.id)
            })?;
        }

        // IMPORTANT: EntityGraphProvider must be tenant-scoped at its adapter
        // layer.
        let graph_nodes = self.entity_provider.get_entity_graph().await?;
        let mut cedar_entities = Vec::with_capacity(graph_nodes.len());

        for node in graph_nodes {
            let type_name = EntityTypeName::from_str(&node.entity_type)?;
            let entity_id = EntityId::from_str(&node.entity_id)?;
            let uid = EntityUid::from_type_name_and_id(type_name, entity_id);

            let mut parents = HashSet::with_capacity(node.parents.len());
            for parent_ref in node.parents {
                let p_type =
                    EntityTypeName::from_str(&parent_ref.entity_type)?;
                let p_id = EntityId::from_str(&parent_ref.entity_id)?;
                parents.insert(EntityUid::from_type_name_and_id(p_type, p_id));
            }

            let entity = Entity::new(uid, HashMap::new(), parents)?;
            cedar_entities.push(entity);
        }

        let entities = Entities::from_entities(cedar_entities, None)?;

        let arc_policy = Arc::new(policy_set);
        let arc_entities = Arc::new(entities);

        let mut cache_guard = self.cache_by_tenant.write().await;
        cache_guard.insert(
            tenant_id.to_string(),
            TenantAuthCache {
                policy_set: Arc::clone(&arc_policy),
                entities: Arc::clone(&arc_entities),
                expires_at: Instant::now() + self.cache_ttl,
            },
        );

        Ok((arc_policy, arc_entities))
    }

    /// Evaluates if a user (principal) can perform an action on a specific
    /// resource (table) given a specific fine-grained context.
    pub async fn is_authorized(
        &self,
        tenant_id: &str,
        user_id: &str,
        action: Action,
        table_name: &str,
        query_context: Option<&QueryContext>,
    ) -> Result<bool> {
        let (policy_set, entities) =
            self.get_cedar_environment(tenant_id).await?;

        let user_type = EntityTypeName::from_str("User")?;
        let user_entity_id = EntityId::from_str(user_id)?;
        let principal =
            EntityUid::from_type_name_and_id(user_type, user_entity_id);

        let action_uid = action.action_uid()?;

        let table_type = EntityTypeName::from_str("Table")?;
        let table_entity_id = EntityId::from_str(table_name)?;
        let resource =
            EntityUid::from_type_name_and_id(table_type, table_entity_id);

        let mut ctx_map = serde_json::Map::new();
        ctx_map.insert("tenant_id".to_string(), json!(tenant_id));

        if let Some(ctx) = query_context {
            if let Some(eid) = &ctx.entity_id {
                ctx_map.insert("entity_id".to_string(), json!(eid));
            }
            if let Some(m) = &ctx.modality {
                ctx_map.insert("modality".to_string(), json!(m));
            }
            if let Some(s) = &ctx.state_type {
                ctx_map.insert("state_type".to_string(), json!(s));
            }
            if let Some(z) = &ctx.gis_zone {
                ctx_map.insert("gis_zone".to_string(), json!(z));
            }
        }

        let cedar_context =
            CedarContext::from_json_value(json!(ctx_map), None)
                .context("Failed to construct Cedar context JSON")?;

        let request = Request::new(
            principal,
            action_uid,
            resource,
            cedar_context,
            None,
        )?;

        let authorizer = Authorizer::new();

        Ok(authorizer
            .is_authorized(&request, &policy_set, &entities)
            .decision() ==
            cedar_policy::Decision::Allow)
    }

    /// Evaluates authorization for multiple tables without specific context.
    pub async fn filter_authorized_resources(
        &self,
        tenant_id: &str,
        user_id: &str,
        action: Action,
        table_names: &[String],
    ) -> Result<HashSet<String>> {
        let (policy_set, entities) =
            self.get_cedar_environment(tenant_id).await?;

        let user_type = EntityTypeName::from_str("User")?;
        let user_entity_id = EntityId::from_str(user_id)?;
        let principal =
            EntityUid::from_type_name_and_id(user_type, user_entity_id);

        let action_uid = action.action_uid()?;
        let table_type = EntityTypeName::from_str("Table")?;

        let authorizer = Authorizer::new();
        let mut allowed_tables = HashSet::with_capacity(table_names.len());

        for name in table_names {
            let table_entity_id = EntityId::from_str(name)?;
            let resource = EntityUid::from_type_name_and_id(
                table_type.clone(),
                table_entity_id,
            );

            let mut ctx_map = serde_json::Map::new();
            ctx_map.insert("tenant_id".to_string(), json!(tenant_id));
            let cedar_context =
                CedarContext::from_json_value(json!(ctx_map), None)
                    .context("Failed to construct Cedar context JSON")?;

            let request = Request::new(
                principal.clone(),
                action_uid.clone(),
                resource,
                cedar_context,
                None,
            )?;

            if authorizer
                .is_authorized(&request, &policy_set, &entities)
                .decision() ==
                cedar_policy::Decision::Allow
            {
                allowed_tables.insert(name.clone());
            }
        }

        Ok(allowed_tables)
    }
}
