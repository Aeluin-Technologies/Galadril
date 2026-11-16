//! Fail-closed authorization over runtime-verified instance and session
//! evidence.

use std::collections::BTreeMap;
use std::future::Future;
use std::sync::Arc;

use cedar_policy::{
    Authorizer, Context, Decision as CedarDecision, Entities, Entity,
    EntityId, EntityUid, Request,
};
use tokio::sync::OwnedMutexGuard;

use super::contract::{
    Authority, Contract, Descriptor, Operation, Route, Versions,
};
use super::flow::{Flow, SinkKind};
use super::{Clearance, Computation, ExecutionContext, kernel};
use crate::{Name, QualifiedName, SymbolRef};

/// The tenant-qualified logical identity of a GOL instance.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Instance {
    /// Tenant established by the runtime, or the tenant of an untrusted
    /// intent.
    pub tenant: Box<str>,
    /// Exact definition version interpreting the instance.
    pub owner: SymbolRef,
    /// The instance's logical ID in its tenant and domain type.
    pub id: Box<str>,
}

impl Instance {
    /// Encodes the tenant, logical type, and instance into a bridge object ID.
    ///
    /// Each component is hex-encoded and delimited. The module version stays
    /// outside the object ID so compatible ontology revisions keep grants.
    #[must_use]
    pub fn object_id(&self) -> Option<String> {
        let namespace = self.owner.key().ontology().to_string();
        let parts = [
            self.tenant.as_ref(),
            namespace.as_str(),
            self.owner.key().name().as_str(),
            self.id.as_ref(),
        ];
        if parts.iter().any(|part| part.is_empty() || part.len() > 510) {
            return None;
        }
        let bytes: usize = parts.iter().map(|part| part.len()).sum();
        let size = bytes * 2 + 3;
        if size > 1024 {
            return None;
        }
        let mut result = String::with_capacity(size);
        for (index, part) in parts.into_iter().enumerate() {
            if index != 0 {
                result.push('-');
            }
            for byte in part.bytes() {
                result.push(hex(byte >> 4));
                result.push(hex(byte & 15));
            }
        }
        Some(result)
    }
}

fn hex(nibble: u8) -> char {
    char::from(if nibble < 10 {
        b'0' + nibble
    } else {
        b'a' + nibble - 10
    })
}

/// An access intent. Values here may originate outside the trusted runtime.
pub struct Access {
    /// The exact target and operation.
    pub route: Route,
    /// The instance requested by the caller.
    pub instance: Instance,
    /// Requested destination. The runtime binds it to the actual sink.
    pub destination: QualifiedName,
    /// Requested purpose, subject to trusted runtime verification.
    pub purpose: QualifiedName,
}

/// An authenticated identity returned by the runtime's IAM adapter.
pub struct Principal {
    /// Verified issuer of the authenticated identity.
    pub issuer: Box<str>,
    /// Verified tenant of the authenticated session.
    pub tenant: Box<str>,
    /// Approved SpiceDB subject type.
    pub subject_type: Box<str>,
    /// Approved SpiceDB subject identifier.
    pub subject_id: Box<str>,
    /// Identity registered by the approved Cedar principal schema.
    pub cedar: EntityUid,
}

/// Runtime evidence tying a resource to its current version and IFC floor.
pub struct InstanceEvidence {
    /// The instance selected by the trusted runtime.
    pub instance: Instance,
    /// Nonempty opaque data revision verified by the runtime.
    pub revision: Box<str>,
    /// Additional IFC constraints supplied by the instance owner.
    pub flow: Option<Flow>,
}

/// The relationship freshness required for this request.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Consistency {
    /// Require a fully consistent check against the relationship authority.
    FullyConsistent,
    /// Require a check satisfying the supplied opaque ZedToken.
    AtLeastAsFresh(Box<str>),
}

/// Evidence minted by the trusted runtime after authenticating the principal
/// and binding the requested operation, instances, purpose and actual sink.
pub struct Evidence {
    /// A principal obtained from verified authentication.
    pub principal: Principal,
    /// Current IAM ceiling, refreshed even when the computation is cached.
    /// This may restrict, but never expand, its original admission ceiling.
    pub max_clearance: Clearance,
    /// The primary resource whose revision was verified.
    pub primary: InstanceEvidence,
    /// Instances needed by prerequisite routes. Each required route needs
    /// evidence for every instance actually used by the operation.
    pub dependencies: BTreeMap<Route, Vec<InstanceEvidence>>,
    /// Static pins currently active in the deployment.
    pub versions: Versions,
    /// Current minimum freshness for permission checks.
    pub consistency: Consistency,
    /// Trusted Cedar context assembled under the deployment's rules.
    pub context: Context,
    /// IFC provenance of contextual values consulted by policy evaluation.
    /// The runtime includes nonpublic clock, location or authentication
    /// inputs.
    pub context_flow: Flow,
    /// Trusted Cedar snapshot for the approved principal and resource UIDs.
    pub entities: Vec<Entity>,
    /// Trusted relationship caveat context in the supported Boolean profile.
    pub relationship_context: BTreeMap<Name, bool>,
    /// Verified destination of the actual release.
    pub destination: QualifiedName,
    /// Verified purpose of the actual operation.
    pub purpose: QualifiedName,
}

/// The host boundary that authenticates a request and verifies runtime state.
///
/// Implementations are part of the trusted computing base. In particular,
/// they establish tenant equality, active revisions, destination identity,
/// context provenance and the complete instance set used by the checked route.
pub trait TrustedRuntime {
    /// Returns the actor-bound computation admitted by the trusted dispatcher.
    /// Missing provenance fails closed; a fresh request is not a fresh domain.
    fn computation(
        &self,
        _principal: &Principal,
    ) -> Option<Arc<ExecutionContext>> {
        None
    }

    /// Returns evidence only for a verified request under current runtime
    /// state.
    fn verify(&self, access: &Access) -> Option<Evidence>;
}

/// A relationship check bound to the caller, actual object and requested
/// freshness.
pub struct PermissionCheck<'a> {
    /// Verified, tenant-qualified principal from IAM.
    pub principal: &'a Principal,
    /// Approved SpiceDB resource type.
    pub resource_type: &'a str,
    /// Canonical bridge object ID.
    pub resource_id: &'a str,
    /// Approved external permission.
    pub permission: &'a Name,
    /// Freshness that must be honored by the authority.
    pub consistency: &'a Consistency,
    /// Approved schema and bridge for this check.
    pub versions: &'a Versions,
    /// Trusted caveat context.
    pub context: &'a BTreeMap<Name, bool>,
}

/// Result of a check against the relationship authority.
#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum PermissionOutcome {
    /// Granted under the reported freshness guarantee.
    Allow(Consistency),
    /// The permission is absent, including after revocation.
    Deny,
    /// Caveats could not be resolved with the trusted context.
    Conditional,
    /// No decision could be obtained.
    Unavailable,
}

/// Trusted adapter to the external relationship authority.
pub trait PermissionProvider {
    /// Checks the exact request and attests to freshness only after it is met.
    fn check(
        &self,
        request: PermissionCheck<'_>,
    ) -> impl Future<Output = PermissionOutcome> + Send;
}

/// The outcome of the complete security gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Decision {
    /// Every mandatory obligation was satisfied for this operation.
    Allow,
    /// One or more required obligations were absent or failed.
    Deny(Denial),
}

/// A bounded category of decision failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Denial {
    /// The runtime could not authenticate or verify the request.
    Runtime,
    /// Active static pins do not match the compiled contract.
    Version,
    /// No compiled route exists for the requested operation.
    Route,
    /// Tenant, logical identity or data revision could not be bound.
    Resource,
    /// Required traversal or expression instances were not supplied.
    Dependency,
    /// The relationship authority denied the permission.
    Relationship,
    /// A caveated permission remains unresolved.
    Conditional,
    /// The relationship provider was unavailable.
    Unavailable,
    /// The check did not meet the requested freshness.
    Freshness,
    /// Cedar denied at least one required policy check.
    Cedar,
    /// Cedar rejected or could not evaluate a request or policy.
    CedarError,
    /// IFC disallows a release to the verified destination and purpose.
    Flow,
}

/// Checks all obligations at a single entry point without caching decisions.
///
/// The adapter, runtime evidence and loaded entities must share the same
/// tenant and freshness boundary. The returned decision covers only this
/// request and concrete dependency set.
pub async fn authorize(
    contract: &Contract,
    access: &Access,
    runtime: &impl TrustedRuntime,
    provider: &impl PermissionProvider,
) -> Decision {
    authorize_routes(
        &contract.authority,
        &contract.descriptors,
        access,
        runtime,
        provider,
    )
    .await
}

pub(super) async fn authorize_routes(
    authority: &Authority,
    routes: &BTreeMap<Route, Descriptor>,
    access: &Access,
    runtime: &impl TrustedRuntime,
    provider: &impl PermissionProvider,
) -> Decision {
    match check(authority, routes, access, runtime, provider, false).await {
        Ok(_guard) => Decision::Allow,
        Err(denial) => {
            tracing::debug!(event.name = "gol.authorization.denied", reason = ?denial);
            Decision::Deny(denial)
        },
    }
}

async fn check(
    authority: &Authority,
    routes: &BTreeMap<Route, Descriptor>,
    access: &Access,
    runtime: &impl TrustedRuntime,
    provider: &impl PermissionProvider,
    preserve_result: bool,
) -> Result<CheckedEffect, Denial> {
    let descriptor = routes.get(&access.route).ok_or(Denial::Route)?;
    let mut evidence = runtime.verify(access).ok_or(Denial::Runtime)?;
    if evidence.versions != authority.versions ||
        authority.versions.ifc != crate::Version::new(2, 0, 0)
    {
        return Err(Denial::Version);
    }
    if evidence.destination != access.destination ||
        evidence.purpose != access.purpose ||
        evidence.principal.cedar.type_name() != &authority.principal ||
        evidence.principal.issuer.is_empty() ||
        evidence.principal.tenant.is_empty() ||
        evidence.principal.subject_type.is_empty() ||
        evidence.principal.subject_id.is_empty()
    {
        return Err(Denial::Runtime);
    }
    if evidence.primary.instance != access.instance ||
        &evidence.primary.instance.owner != access.route.owner() ||
        evidence.primary.instance.tenant != evidence.principal.tenant ||
        evidence.primary.revision.is_empty()
    {
        return Err(Denial::Resource);
    }
    if matches!(&evidence.consistency, Consistency::AtLeastAsFresh(token) if token.is_empty())
    {
        return Err(Denial::Freshness);
    }
    let sink = authority
        .sinks
        .get(&evidence.destination)
        .ok_or(Denial::Flow)?;
    if sink.kind == SinkKind::External &&
        !matches!(
            access.route.operation,
            Operation::Transfer | Operation::DescribeTransfer
        )
    {
        return Err(Denial::Flow);
    }
    let computation = runtime
        .computation(&evidence.principal)
        .ok_or(Denial::Flow)?;
    let mut state = computation
        .lock(&evidence.principal)
        .await
        .map_err(|_| Denial::Flow)?;
    observe(&mut state, &evidence.context_flow, &evidence.max_clearance)?;
    // A metadata filter can reject a static candidate without consulting
    // its value or policy. Such a rejection must match an absent candidate.
    if matches!(
        access.route.operation,
        Operation::Describe | Operation::DescribeTransfer
    ) && (!evidence.max_clearance.admits(&descriptor.flow) ||
        !state.max_clearance().admits(&descriptor.flow) ||
        !evidence.max_clearance.admits(&descriptor.control_flow) ||
        !state.max_clearance().admits(&descriptor.control_flow))
    {
        return Err(Denial::Flow);
    }
    // Policy inputs influence denial, absence and error results as well as
    // successful values. Account for them before invoking either engine.
    observe(
        &mut state,
        &descriptor.control_flow,
        &evidence.max_clearance,
    )?;
    for route in &descriptor.dependencies {
        let dependency = routes.get(route).ok_or(Denial::Dependency)?;
        observe(
            &mut state,
            &dependency.control_flow,
            &evidence.max_clearance,
        )?;
    }
    if !sink.admits(
        state.current_label().map_err(|_| Denial::Flow)?,
        &evidence.destination,
        &evidence.purpose,
    ) {
        return Err(Denial::Flow);
    }
    let entities = Entities::from_entities(
        std::mem::take(&mut evidence.entities),
        Some(&authority.schema),
    )
    .map_err(|_| Denial::CedarError)?;
    let mut check = RequestCheck {
        authority,
        evidence: &evidence,
        entities: &entities,
        computation: &mut state,
    };
    // The prerequisites are transitively compiled and run against their own
    // instance evidence. A permission on the root cannot satisfy another hop.
    check
        .instance(&access.route, descriptor, &evidence.primary, provider)
        .await?;
    for route in &descriptor.dependencies {
        let instances = evidence
            .dependencies
            .get(route)
            .filter(|instances| !instances.is_empty())
            .ok_or(Denial::Dependency)?;
        let dependency = routes.get(route).ok_or(Denial::Dependency)?;
        for instance in instances {
            check
                .instance(route, dependency, instance, provider)
                .await?;
        }
    }
    // A later dependency read constrains every earlier proposed write.
    check.write(&access.route, descriptor, &evidence.primary)?;
    for route in &descriptor.dependencies {
        let dependency = routes.get(route).ok_or(Denial::Dependency)?;
        for instance in evidence.dependencies.get(route).into_iter().flatten()
        {
            check.write(route, dependency, instance)?;
        }
    }
    if !sink.admits(
        state.current_label().map_err(|_| Denial::Flow)?,
        &evidence.destination,
        &evidence.purpose,
    ) {
        return Err(Denial::Flow);
    }
    let mut outcome = if preserve_result {
        Some(state.current_label().map_err(|_| Denial::Flow)?.clone())
    } else {
        None
    };
    if let Some(label) = &mut outcome {
        for (route, descriptor, instance) in
            std::iter::once((&access.route, descriptor, &evidence.primary))
                .chain(evidence.dependencies.iter().flat_map(
                    |(route, instances)| {
                        routes.get(route).into_iter().flat_map(
                            move |descriptor| {
                                instances.iter().map(move |instance| {
                                    (route, descriptor, instance)
                                })
                            },
                        )
                    },
                ))
        {
            if route.operation == Operation::Write {
                *label = label.join(&descriptor.flow);
                if let Some(floor) = &instance.flow {
                    *label = label.join(floor);
                }
            }
        }
    }
    Ok(CheckedEffect {
        state,
        computation,
        outcome,
        max_clearance: evidence.max_clearance,
    })
}

fn observe(
    state: &mut Computation,
    flow: &Flow,
    maximum: &Clearance,
) -> Result<(), Denial> {
    if !maximum.admits(state.current_label().map_err(|_| Denial::Flow)?) ||
        !maximum.admits(flow)
    {
        state.poison();
        return Err(Denial::Flow);
    }
    state.observe(flow).map_err(|_| Denial::Flow)
}

struct CheckedEffect {
    state: OwnedMutexGuard<Computation>,
    computation: Arc<ExecutionContext>,
    outcome: Option<Flow>,
    max_clearance: Clearance,
}

struct RequestCheck<'a> {
    authority: &'a Authority,
    evidence: &'a Evidence,
    entities: &'a Entities,
    computation: &'a mut Computation,
}

impl RequestCheck<'_> {
    fn write(
        &self,
        route: &Route,
        descriptor: &Descriptor,
        evidence: &InstanceEvidence,
    ) -> Result<(), Denial> {
        if route.operation != Operation::Write {
            return Ok(());
        }
        if descriptor.compound {
            return Err(Denial::Flow);
        }
        let effective = evidence
            .flow
            .as_ref()
            .map(|floor| descriptor.flow.join(floor));
        let flow = effective.as_ref().unwrap_or(&descriptor.flow);
        self.computation
            .check_write(
                flow,
                &self.evidence.destination,
                &self.evidence.purpose,
            )
            .map_err(|_| Denial::Flow)
    }

    async fn instance(
        &mut self,
        route: &Route,
        descriptor: &Descriptor,
        evidence: &InstanceEvidence,
        provider: &impl PermissionProvider,
    ) -> Result<(), Denial> {
        let instance = &evidence.instance;
        if &instance.owner != route.owner() ||
            instance.tenant != self.evidence.principal.tenant ||
            evidence.revision.is_empty()
        {
            return Err(Denial::Resource);
        }
        let object_id = instance.object_id().ok_or(Denial::Resource)?;
        let sink = self
            .authority
            .sinks
            .get(&self.evidence.destination)
            .ok_or(Denial::Flow)?;
        let effective = evidence
            .flow
            .as_ref()
            .map(|floor| descriptor.flow.join(floor));
        let flow = effective.as_ref().unwrap_or(&descriptor.flow);
        let flow_allowed = sink.admits(
            flow,
            &self.evidence.destination,
            &self.evidence.purpose,
        );
        if !flow_allowed {
            return Err(Denial::Flow);
        }
        if !descriptor.fields.is_empty() &&
            let Some(floor) = &evidence.flow
        {
            observe(self.computation, floor, &self.evidence.max_clearance)?;
        }
        if !sink.admits(
            self.computation.current_label().map_err(|_| Denial::Flow)?,
            &self.evidence.destination,
            &self.evidence.purpose,
        ) {
            return Err(Denial::Flow);
        }
        let resource = EntityUid::from_type_name_and_id(
            descriptor.resource_type.clone(),
            EntityId::new(object_id.as_str()),
        );
        if !descriptor.fields.is_empty() &&
            self.entities.get(&resource).is_none()
        {
            return Err(Denial::CedarError);
        }
        for permission in &descriptor.bindings {
            let binding = &permission.binding.relationship;
            let outcome = provider
                .check(PermissionCheck {
                    principal: &self.evidence.principal,
                    resource_type: &binding.resource_type,
                    resource_id: &object_id,
                    permission: &binding.permission,
                    consistency: &self.evidence.consistency,
                    versions: &self.evidence.versions,
                    context: &self.evidence.relationship_context,
                })
                .await;
            let relationship_allowed = match outcome {
                PermissionOutcome::Allow(consistency)
                    if consistency == self.evidence.consistency =>
                {
                    true
                },
                PermissionOutcome::Allow(_) => return Err(Denial::Freshness),
                PermissionOutcome::Deny => return Err(Denial::Relationship),
                PermissionOutcome::Conditional => {
                    return Err(Denial::Conditional);
                },
                PermissionOutcome::Unavailable => {
                    return Err(Denial::Unavailable);
                },
            };
            let request = Request::new(
                self.evidence.principal.cedar.clone(),
                permission.action.clone(),
                resource.clone(),
                self.evidence.context.clone(),
                Some(&self.authority.schema),
            )
            .map_err(|_| Denial::CedarError)?;
            for policies in std::iter::once(&self.authority.policies)
                .chain(&permission.groups)
            {
                let result = Authorizer::new().is_authorized(
                    &request,
                    policies,
                    self.entities,
                );
                if result.diagnostics().errors().next().is_some() {
                    return Err(Denial::CedarError);
                }
                if !kernel::obligations(
                    relationship_allowed,
                    result.decision() == CedarDecision::Allow,
                    flow_allowed,
                ) {
                    return Err(Denial::Cedar);
                }
            }
        }
        if route.operation == Operation::Write {
            self.write(route, descriptor, evidence)?;
        } else if matches!(
            route.operation,
            Operation::Read |
                Operation::Transfer |
                Operation::Describe |
                Operation::DescribeTransfer
        ) {
            observe(self.computation, flow, &self.evidence.max_clearance)?;
            if !sink.admits(
                self.computation.current_label().map_err(|_| Denial::Flow)?,
                &self.evidence.destination,
                &self.evidence.purpose,
            ) {
                return Err(Denial::Flow);
            }
        }
        Ok(())
    }
}

/// A bounded effect failure without payloads, resource names or storage
/// errors.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffectError {
    /// A mandatory authority or information-flow obligation failed.
    Denied(Denial),
    /// The trusted effect implementation did not complete successfully.
    Unavailable,
}
impl std::fmt::Display for EffectError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Protected effect unavailable")
    }
}
impl std::error::Error for EffectError {}

/// Checks fresh policies and holds the computation through the actual effect.
///
/// The callback is trusted backend code bound to this exact resource and
/// destination. It must use transactional storage where atomicity is required.
/// Nested calls with the same computation are forbidden; dependencies belong
/// in the compiled route, before the effect begins.
pub async fn perform<T, E, F: Future<Output = Result<T, E>>>(
    contract: &Contract,
    access: &Access,
    runtime: &impl TrustedRuntime,
    provider: &impl PermissionProvider,
    effect: impl FnOnce() -> F,
) -> Result<EffectReceipt<T>, EffectError> {
    perform_routes(
        &contract.authority,
        &contract.descriptors,
        access,
        runtime,
        provider,
        effect,
    )
    .await
}

pub(super) async fn perform_routes<T, E, F: Future<Output = Result<T, E>>>(
    authority: &Authority,
    routes: &BTreeMap<Route, Descriptor>,
    access: &Access,
    runtime: &impl TrustedRuntime,
    provider: &impl PermissionProvider,
    effect: impl FnOnce() -> F,
) -> Result<EffectReceipt<T>, EffectError> {
    if !matches!(
        access.route.operation,
        Operation::Write |
            Operation::Execute |
            Operation::Transfer |
            Operation::DescribeTransfer
    ) {
        return Err(EffectError::Denied(Denial::Route));
    }
    if access.route.operation == Operation::Write &&
        matches!(access.route.target, crate::Reference::Symbol(_))
    {
        return Err(EffectError::Denied(Denial::Route));
    }
    let guard = check(
        authority,
        routes,
        access,
        runtime,
        provider,
        true,
    )
    .await
    .map_err(|denial| {
        tracing::debug!(event.name = "gol.effect.denied", reason = ?denial);
        EffectError::Denied(denial)
    })?;
    let result = effect().await;
    let result = match result {
        Ok(value) => Ok(value),
        Err(_error) => {
            tracing::warn!(event.name = "gol.effect.unavailable");
            Err(EffectError::Unavailable)
        },
    };
    let receipt = EffectReceipt {
        result,
        flow: guard.outcome.ok_or(EffectError::Denied(Denial::Flow))?,
        origin: guard.computation,
        access: Access {
            route: access.route.clone(),
            instance: access.instance.clone(),
            destination: access.destination.clone(),
            purpose: access.purpose.clone(),
        },
    };
    drop(guard.state);
    Ok(receipt)
}

/// A stored result or error whose observation retains destination
/// restrictions.
///
/// It is deliberately neither serializable nor a cross-calculation grant.
/// Successful admission does not assert successful storage: inspect the
/// receipt under fresh read permissions to observe its protected status.
pub struct EffectReceipt<T> {
    result: Result<T, EffectError>,
    flow: Flow,
    origin: Arc<ExecutionContext>,
    access: Access,
}

impl<T> EffectReceipt<T> {
    #[cfg(feature = "remote")]
    pub(crate) fn tenant(&self) -> &str {
        &self.access.instance.tenant
    }

    /// Rechecks access and raises the originating calculation before
    /// disclosure. A field mutation's result needs its read route,
    /// independently of write.
    pub async fn inspect(
        &self,
        contract: &Contract,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<&Result<T, EffectError>, EffectError> {
        self.inspect_routes(
            &contract.authority,
            &contract.descriptors,
            runtime,
            provider,
        )
        .await
    }

    pub(crate) fn observation_route(&self) -> Route {
        let operation = if self.access.route.operation == Operation::Write {
            Operation::Read
        } else {
            self.access.route.operation
        };
        Route::new(self.access.route.target.clone(), operation)
    }

    pub(super) async fn inspect_routes(
        &self,
        authority: &Authority,
        routes: &BTreeMap<Route, Descriptor>,
        runtime: &impl TrustedRuntime,
        provider: &impl PermissionProvider,
    ) -> Result<&Result<T, EffectError>, EffectError> {
        let access = Access {
            route: self.observation_route(),
            instance: self.access.instance.clone(),
            destination: self.access.destination.clone(),
            purpose: self.access.purpose.clone(),
        };
        let mut checked =
            check(authority, routes, &access, runtime, provider, false)
                .await
                .map_err(EffectError::Denied)?;
        if !Arc::ptr_eq(&self.origin, &checked.computation) {
            return Err(EffectError::Denied(Denial::Runtime));
        }
        observe(&mut checked.state, &self.flow, &checked.max_clearance)
            .map_err(EffectError::Denied)?;
        let sink = authority
            .sinks
            .get(&self.access.destination)
            .ok_or(EffectError::Denied(Denial::Flow))?;
        if !sink.admits(
            checked
                .state
                .current_label()
                .map_err(|_| EffectError::Denied(Denial::Flow))?,
            &self.access.destination,
            &self.access.purpose,
        ) {
            return Err(EffectError::Denied(Denial::Flow));
        }
        Ok(&self.result)
    }
}

impl<T> std::fmt::Debug for EffectReceipt<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("EffectReceipt { .. }")
    }
}
