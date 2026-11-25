//! Bind metadata evidence to an approved generation and authenticated actor.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use anyhow::{Context as _, Result, ensure};
use cedar_policy::{Context, Entity, EntityId, EntityUid};
use chrono::Timelike as _;
use galadril_ontology::archive::remote::RemoteArchive;
use galadril_ontology::authorization::{
    Access, Consistency, Descriptor, Evidence, ExecutionContext, Flow,
    Instance, InstanceEvidence, Operation, Principal, Route, TrustedRuntime,
    Versions,
};
use galadril_ontology::export::{Projection, Selection};
use galadril_ontology::{Name, Reference, SymbolRef};
use galadril_registry::gol::GolAuthorityConfig;

use super::admission::Actor;
use crate::application::usecases::authorization::QueryContext;

pub(super) struct Snapshot<'a> {
    actor: &'a Actor,
    state: Arc<ExecutionContext>,
    versions: Versions,
    consistency: Consistency,
    requirements: BTreeMap<Route, Descriptor>,
    revision: String,
    context: Context,
    context_flow: Flow,
    entities: Vec<Entity>,
    destination: galadril_ontology::QualifiedName,
    purpose: galadril_ontology::QualifiedName,
    instance_id: Box<str>,
    instance_flow: Option<Flow>,
}

impl<'a> Snapshot<'a> {
    /// Missing resource attributes or contextual provenance close the request;
    /// proposed values never masquerade as trusted stored resource attributes.
    pub async fn prepare(
        archive: &RemoteArchive,
        access: &Access,
        actor: &'a Actor,
        state: Arc<ExecutionContext>,
        facts: &QueryContext,
        authority: &GolAuthorityConfig,
        context_flow: &Flow,
    ) -> Result<Self> {
        ensure!(
            matches!(
                access.route.operation,
                Operation::Describe | Operation::DescribeTransfer
            ),
            "GOL metadata route required"
        );
        ensure!(
            access.instance.id.as_ref() == "__schema" &&
                access.instance.tenant == actor.tenant,
            "GOL metadata binding unavailable"
        );
        Self::build(
            archive,
            access,
            actor,
            state,
            facts,
            authority,
            context_flow,
            &archive.generation().to_string(),
            None,
            true,
        )
        .await
    }

    /// Data facts require a concrete revision and explicit same-instance
    /// paths. Cross-owner traversals need an authoritative resolver, never
    /// guessed IDs.
    pub async fn prepare_data(
        archive: &RemoteArchive,
        access: &Access,
        actor: &'a Actor,
        state: Arc<ExecutionContext>,
        facts: &QueryContext,
        authority: &GolAuthorityConfig,
        context_flow: &Flow,
        revision: &str,
        flow: Option<&Flow>,
    ) -> Result<Self> {
        ensure!(
            access.instance.id.as_ref() != "__schema" &&
                !revision.is_empty() &&
                access.instance.tenant == actor.tenant,
            "GOL data binding unavailable"
        );
        Self::build(
            archive,
            access,
            actor,
            state,
            facts,
            authority,
            context_flow,
            revision,
            flow,
            false,
        )
        .await
    }

    async fn build(
        archive: &RemoteArchive,
        access: &Access,
        actor: &'a Actor,
        state: Arc<ExecutionContext>,
        facts: &QueryContext,
        authority: &GolAuthorityConfig,
        context_flow: &Flow,
        revision: &str,
        flow: Option<&Flow>,
        metadata: bool,
    ) -> Result<Self> {
        let requirements = archive.requirements(&access.route).await?;
        let mut resources = BTreeMap::<SymbolRef, EntityUid>::new();
        for (route, descriptor) in &requirements {
            ensure!(
                descriptor.fields().is_empty(),
                "GOL trusted resource attributes unavailable"
            );
            let owner = super::owner(&route.target)?;
            ensure!(
                metadata || owner == &access.instance.owner,
                "GOL traversal provenance unavailable"
            );
            let instance = Instance {
                tenant: actor.tenant.clone(),
                owner: owner.clone(),
                id: access.instance.id.clone(),
            };
            let id =
                instance.object_id().context("GOL resource unavailable")?;
            resources.insert(
                owner.clone(),
                EntityUid::from_type_name_and_id(
                    descriptor.cedar_resource_type().clone(),
                    EntityId::new(id),
                ),
            );
        }
        let mut entities = Vec::with_capacity(resources.len() + 1);
        entities.push(Entity::new(
            actor.cedar.clone(),
            HashMap::new(),
            HashSet::new(),
        )?);
        for uid in resources.into_values() {
            entities.push(Entity::new(uid, HashMap::new(), HashSet::new())?);
        }
        let mut context = serde_json::Map::new();
        for name in authority.cedar_context.keys() {
            let value = match name.as_str() {
                "mfa" => serde_json::Value::Bool(facts.mfa_verified),
                "hour_utc" => serde_json::json!(chrono::Utc::now().hour()),
                "location" => serde_json::Value::String(
                    facts
                        .region
                        .clone()
                        .context("GOL trusted location unavailable")?,
                ),
                _ => anyhow::bail!("GOL trusted context unavailable"),
            };
            context.insert(name.to_string(), value);
        }
        Ok(Self {
            actor,
            state,
            versions: archive.versions().clone(),
            requirements,
            revision: revision.to_owned(),
            consistency: Consistency::FullyConsistent,
            context: Context::from_json_value(
                serde_json::Value::Object(context),
                None,
            )?,
            context_flow: context_flow.clone(),
            entities,
            destination: access.destination.clone(),
            purpose: access.purpose.clone(),
            instance_id: access.instance.id.clone(),
            instance_flow: flow.cloned(),
        })
    }

    /// Applies a revision acquired by the relationship authority for this
    /// decision.
    pub fn at_consistency(mut self, consistency: &Consistency) -> Self {
        self.consistency = consistency.clone();
        self
    }

    /// Adds only explicitly proposed same-instance fields to a data snapshot.
    pub async fn proposed_fields(
        &mut self,
        archive: &RemoteArchive,
        fields: &BTreeSet<Name>,
        owner: &SymbolRef,
    ) -> Result<()> {
        for name in fields {
            let requirements = archive
                .requirements(&Route::new(
                    crate::application::usecases::gol::Reference::Field(
                        galadril_ontology::FieldRef::new(
                            owner.clone(),
                            name.clone(),
                        ),
                    ),
                    Operation::Write,
                ))
                .await?;
            ensure!(
                requirements.iter().all(|(route, descriptor)| super::owner(
                    &route.target
                )
                .is_ok_and(|required| required == owner) &&
                    descriptor.fields().is_empty()),
                "GOL proposed field needs authoritative state"
            );
            self.requirements.extend(requirements);
        }
        Ok(())
    }

    /// Definitions are approved separately, including self references. Their
    /// exact instances remain metadata objects rather than guessed data IDs.
    pub fn projection(
        &self,
        access: Access,
        fields: BTreeSet<Name>,
    ) -> Result<Projection> {
        let mut projection = Projection::new(Selection::new(access, fields)?);
        let owners = self
            .requirements
            .keys()
            .map(|route| super::owner(&route.target).cloned())
            .collect::<Result<BTreeSet<_>>>()?;
        for owner in owners {
            let access = Access {
                route: Route::new(
                    owner.clone(),
                    projection.root.access.route.operation,
                ),
                instance: Instance {
                    tenant: self.actor.tenant.clone(),
                    owner: owner.clone(),
                    id: self.instance_id.clone(),
                },
                destination: self.destination.clone(),
                purpose: self.purpose.clone(),
            };
            let members = self
                .requirements
                .keys()
                .filter_map(|route| match &route.target {
                    Reference::Field(field) if field.owner == owner => {
                        Some(field.name.clone())
                    },
                    _ => None,
                })
                .collect::<BTreeSet<_>>();
            projection
                .definitions
                .insert(owner, Selection::new(access, members)?);
        }
        Ok(projection)
    }

    fn resource(&self, owner: &SymbolRef) -> InstanceEvidence {
        InstanceEvidence {
            instance: Instance {
                tenant: self.actor.tenant.clone(),
                owner: owner.clone(),
                id: self.instance_id.clone(),
            },
            revision: self.revision.clone().into_boxed_str(),
            flow: self.instance_flow.clone(),
        }
    }
}

/// Schema metadata and business instances retain distinct object identities.
pub(super) struct Pair<'a> {
    pub metadata: Snapshot<'a>,
    pub data: Snapshot<'a>,
}

impl TrustedRuntime for Pair<'_> {
    fn computation(
        &self,
        principal: &Principal,
    ) -> Option<Arc<ExecutionContext>> {
        self.data.computation(principal)
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        if access.instance.id.as_ref() == "__schema" {
            self.metadata.verify(access)
        } else {
            self.data.verify(access)
        }
    }
}

impl TrustedRuntime for Snapshot<'_> {
    fn computation(
        &self,
        principal: &Principal,
    ) -> Option<Arc<ExecutionContext>> {
        (principal.tenant == self.actor.tenant &&
            principal.subject_id == self.actor.subject_id &&
            principal.subject_type == self.actor.subject_type &&
            principal.issuer == self.actor.issuer)
            .then(|| Arc::clone(&self.state))
    }

    fn verify(&self, access: &Access) -> Option<Evidence> {
        let descriptor = self.requirements.get(&access.route)?;
        let owner = super::owner(&access.route.target).ok()?;
        if access.instance.tenant != self.actor.tenant ||
            access.instance.id != self.instance_id ||
            &access.instance.owner != owner ||
            access.destination != self.destination ||
            access.purpose != self.purpose
        {
            return None;
        }
        let dependencies = descriptor
            .dependencies()
            .iter()
            .map(|route| {
                Some((
                    route.clone(),
                    vec![self.resource(super::owner(&route.target).ok()?)],
                ))
            })
            .collect::<Option<BTreeMap<_, _>>>()?;
        Some(Evidence {
            principal: self.actor.principal(),
            max_clearance: self.actor.clearance.clone(),
            primary: self.resource(owner),
            dependencies,
            versions: self.versions.clone(),
            consistency: self.consistency.clone(),
            context: self.context.clone(),
            context_flow: self.context_flow.clone(),
            entities: self.entities.clone(),
            relationship_context: BTreeMap::new(),
            destination: self.destination.clone(),
            purpose: self.purpose.clone(),
        })
    }
}
