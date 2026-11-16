//! Compile offline obligations before exposing a decision-capable contract.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Display, Formatter};

use cedar_policy::EntityUid;
use semver::Version;

use super::cedar::{self, CompiledCedar};
use super::contract::{
    Authority, Contract, Descriptor, Operation, Permission, Route, Versions,
    owner,
};
use super::environment::{CedarInput, RelationshipInput, SecurityEnvironment};
use super::propagation::{self, Propagation, declaration, inherited};
use crate::semantic::{EffectPath, PermissionRef, Project};
use crate::{
    AccessBinding, ContextPolicy, Declaration, FieldKind, FieldRef, Name,
    Reference, RelationshipPermission,
};

/// Stable categories of static security issues.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum IssueKind {
    /// The route or projection refers to a missing or incompatible target.
    InvalidTarget,
    /// An explicitly required operation route has no host mapping.
    MissingRoute,
    /// A required local permission has incomplete bindings.
    MissingBinding,
    /// No approved SpiceDB schema was supplied.
    MissingRelationshipSchema,
    /// The SpiceDB resource or permission is incompatible with the approved
    /// schema.
    InvalidRelationshipBinding,
    /// No approved Cedar input was supplied.
    MissingCedarBundle,
    /// An external Cedar schema declaration remains unresolved.
    UnlinkedCedarSchema,
    /// A native or generated Cedar schema is invalid.
    InvalidCedarSchema,
    /// Cedar rejected a policy during strict validation.
    InvalidCedarPolicy,
    /// A value or operation lacks a supported Cedar lowering.
    UnsupportedCedarType,
    /// An IFC constraint could not be conservatively interpreted.
    InvalidFlow,
    /// An external data type's adapter approval is missing.
    MissingAdapter,
    /// An emitted effect has no checked destination contract in this profile.
    UnlinkedEffect,
}

/// A static issue associated with a logical GOL target when available.
#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Issue {
    kind: IssueKind,
    target: Option<Box<Reference>>,
    message: Box<str>,
}

impl Issue {
    pub(super) fn new(
        kind: IssueKind,
        target: Option<Reference>,
        message: impl Into<Box<str>>,
    ) -> Self {
        Self {
            kind,
            target: target.map(Box::new),
            message: message.into(),
        }
    }

    /// Returns the category for programmatic reporting.
    #[must_use]
    pub fn kind(&self) -> IssueKind {
        self.kind
    }

    /// Associates the issue with the declaration or property being compiled.
    #[must_use]
    pub fn target(&self) -> Option<&Reference> {
        self.target.as_deref()
    }
}

impl Display for Issue {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for Issue {}

/// Result of static security compilation. Pending and invalid states carry
/// diagnostics only, so they cannot be used to authorize data access.
#[non_exhaustive]
pub enum Compilation {
    /// A mandatory obligation conflicts with its schema or local declaration.
    Invalid(Vec<Issue>),
    /// Mandatory approval, linkage, or route information is still missing.
    PendingExternalValidation(Vec<Issue>),
    /// All static checks required by this profile passed.
    Validated(Box<Contract>),
}

impl Compilation {
    /// Returns a contract eligible for publication under this static profile.
    /// Deployment must still install the mandatory runtime gate.
    #[must_use]
    pub fn publishable(&self) -> Option<&Contract> {
        match self {
            Self::Validated(contract) => Some(contract),
            _ => None,
        }
    }

    /// Returns the issues blocking static publication.
    #[must_use]
    pub fn issues(&self) -> &[Issue] {
        match self {
            Self::Invalid(issues) |
            Self::PendingExternalValidation(issues) => issues,
            Self::Validated(_) => &[],
        }
    }
}

/// Validates local GOL obligations against approved schemas and compiles an
/// immutable decision contract. The caller supplies all authority metadata.
///
/// # Examples
///
/// An environment with no approved authority inputs cannot publish a
/// contract, even when the ontology has passed semantic analysis.
///
/// ```
/// use galadril_ontology::{
///     Classification, ExternalTypeRegistry, Version, VersionedName,
///     authorization::{AuthorityRevision, Compilation, Flow, SecurityEnvironment, compile},
///     semantic::{Environment, analyze_project},
///     syntax::parse_source,
/// };
///
/// # fn main() -> Result<(), Box<dyn std::error::Error>> {
/// let source = parse_source(
///     "example.gol",
///     r#"ontology example version "1.0.0" { entity Record { field title: String; } }"#,
/// )?;
/// let registry = ExternalTypeRegistry::default();
/// let project = analyze_project(&[source], &Environment::new(&registry))?;
/// let revision = AuthorityRevision::new(
///     VersionedName::new("example.deployment".parse()?, Version::new(1, 0, 0)),
///     [7; 32],
/// );
/// let environment = SecurityEnvironment::new(revision, Flow::new(Classification::Internal));
/// let result = compile(&project, &environment);
/// assert!(matches!(result, Compilation::PendingExternalValidation(_)));
/// assert!(result.publishable().is_none());
/// # Ok(())
/// # }
/// ```
#[must_use]
pub fn compile(
    project: &Project,
    environment: &SecurityEnvironment,
) -> Compilation {
    let result = match compile_checked(project, environment) {
        Ok(contract) => Compilation::Validated(Box::new(contract)),
        Err(mut issues) => {
            issues.sort_unstable();
            issues.dedup();
            if issues.iter().all(|issue| pending(issue.kind)) {
                Compilation::PendingExternalValidation(issues)
            } else {
                Compilation::Invalid(issues)
            }
        },
    };
    for issue in result.issues() {
        tracing::debug!(event.name = "gol.security.compilation_issue", kind = ?issue.kind());
    }
    result
}

fn pending(kind: IssueKind) -> bool {
    matches!(
        kind,
        IssueKind::MissingRoute |
            IssueKind::MissingBinding |
            IssueKind::MissingRelationshipSchema |
            IssueKind::MissingCedarBundle |
            IssueKind::UnlinkedCedarSchema |
            IssueKind::MissingAdapter |
            IssueKind::UnlinkedEffect
    )
}

fn compile_checked(
    project: &Project,
    environment: &SecurityEnvironment,
) -> Result<Contract, Vec<Issue>> {
    let propagation = propagation::propagate(project, environment)
        .map_err(|issue| vec![issue])?;
    let mut issues = unresolved(project, environment);
    if environment.relationship.is_none() {
        issues.push(Issue::new(
            IssueKind::MissingRelationshipSchema,
            None,
            "An approved SpiceDB schema and bridge are required",
        ));
    }
    if environment.cedar.is_none() {
        issues.push(Issue::new(
            IssueKind::MissingCedarBundle,
            None,
            "An approved Cedar bundle is required",
        ));
    }
    let (Some(relationship), Some(input)) =
        (&environment.relationship, &environment.cedar)
    else {
        return Err(issues);
    };
    let cedar = cedar::compile(project, input).map_err(|issue| {
        let mut result = std::mem::take(&mut issues);
        result.push(issue);
        result
    })?;
    let mut bindings = BTreeMap::new();
    for (target, security) in project.security() {
        for (name, binding) in &security.permissions {
            let permission = PermissionRef {
                target: owner(target).clone(),
                name: name.clone(),
            };
            match bind(
                &permission,
                security.resource.as_deref(),
                binding,
                relationship,
                input,
                &cedar,
            ) {
                Ok(binding) => {
                    bindings.insert(permission, binding);
                },
                Err(issue) => issues.push(issue),
            }
        }
    }
    if !issues.is_empty() {
        return Err(issues);
    }

    let mut descriptors = BTreeMap::new();
    let compile = RouteCompiler {
        project,
        environment,
        cedar: &cedar,
        input,
        propagation: &propagation,
        bindings: &bindings,
    };
    for route in environment.routes.keys() {
        match compile.descriptor(route) {
            Ok(descriptor) => {
                descriptors.insert(route.clone(), descriptor);
            },
            Err(issue) => issues.push(issue),
        }
    }
    if !issues.is_empty() {
        return Err(issues);
    }
    check_policy_coverage(project, &descriptors, &mut issues);
    if !issues.is_empty() {
        return Err(issues);
    }
    Ok(Contract {
        authority: Authority {
            versions: Versions {
                ontologies: project
                    .modules()
                    .values()
                    .map(|module| module.module().clone())
                    .collect(),
                contract: environment.revision.clone(),
                relationship_schema: relationship.schema.clone(),
                bridge: relationship.bridge.clone(),
                cedar: input.revision.clone(),
                ifc: Version::new(2, 0, 0),
            },
            sinks: environment.sinks.clone(),
            schema: cedar.schema,
            policies: cedar.policies,
            principal: input.principal.clone(),
            #[cfg(feature = "archive")]
            fragments: cedar.fragments,
        },
        descriptors,
        flows: propagation.flows,
        adapters: project
            .external_imports()
            .values()
            .flatten()
            .cloned()
            .collect(),
    })
}

fn check_policy_coverage(
    project: &Project,
    descriptors: &BTreeMap<Route, Descriptor>,
    issues: &mut Vec<Issue>,
) {
    // An owner policy may be reached through a field's guard, so use the
    // effective permissions from the compiled routes to check coverage.
    let mut active: BTreeMap<Reference, BTreeSet<&Name>> = BTreeMap::new();
    for (route, descriptor) in descriptors {
        active
            .entry(route.target.clone())
            .or_default()
            .extend(descriptor.local_permissions());
        if let Reference::Field(field) = &route.target {
            active
                .entry(Reference::Symbol(field.owner.clone()))
                .or_default()
                .extend(descriptor.local_permissions());
        }
    }
    for (target, security) in project.security() {
        for policy in &security.policies {
            if let Some(name) = &policy.permission &&
                !active
                    .get(target)
                    .is_some_and(|names| names.contains(name))
            {
                issues.push(Issue::new(
                    IssueKind::MissingRoute,
                    Some(target.clone()),
                    format!(
                        "No approved route activates policy permission {name}"
                    ),
                ));
            }
        }
    }
}

fn unresolved(
    project: &Project,
    environment: &SecurityEnvironment,
) -> Vec<Issue> {
    let mut issues = Vec::new();
    for module in environment.module_floors.keys() {
        if !project.modules().contains_key(module) {
            issues.push(Issue::new(
                IssueKind::InvalidTarget,
                None,
                format!("The IFC floor names unknown ontology {module}"),
            ));
        }
    }
    for id in project.external_imports().values().flatten() {
        if !environment.approved_types.contains(id) {
            issues.push(Issue::new(
                IssueKind::MissingAdapter,
                None,
                format!("No approval for adapter of {id}"),
            ));
        }
    }
    for (owner, effects) in project.effects() {
        if effects
            .iter()
            .any(|effect| matches!(effect, EffectPath::Emit(_)))
        {
            issues.push(Issue::new(
                IssueKind::UnlinkedEffect,
                Some(owner.clone().into()),
                "An emitted effect requires a checked transfer contract",
            ));
        }
    }
    for (target, security) in project.security() {
        if !(security.policies.is_empty() && security.requires.is_empty()) &&
            !environment
                .routes
                .keys()
                .any(|route| &route.target == target)
        {
            issues.push(Issue::new(
                IssueKind::MissingRoute,
                Some(target.clone()),
                "The declared guard or policy has no approved operation route",
            ));
        }
    }
    issues
}

fn bind(
    permission: &PermissionRef,
    resource: Option<&str>,
    binding: &crate::semantic::Binding,
    relationship: &RelationshipInput,
    input: &CedarInput,
    cedar: &CompiledCedar,
) -> Result<(AccessBinding, EntityUid), Issue> {
    let target = Reference::Symbol(permission.target.clone());
    let (Some(resource), Some(rebac), Some(action)) =
        (resource, &binding.spicedb, &binding.cedar)
    else {
        return Err(Issue::new(
            IssueKind::MissingBinding,
            Some(target),
            format!(
                "Permission {} requires a complete SpiceDB and Cedar binding",
                permission.name
            ),
        ));
    };
    if resource.len() > 128 ||
        !resource.split('/').all(|part| {
            part.as_bytes().first().is_some_and(u8::is_ascii_lowercase) &&
                valid_relationship_name(part)
        }) ||
        !valid_relationship_name(rebac.as_str()) ||
        !relationship.resources.get(&permission.target).is_some_and(
            |approved| {
                approved.resource_type.as_ref() == resource &&
                    approved.permissions.contains(rebac)
            },
        )
    {
        return Err(Issue::new(
            IssueKind::InvalidRelationshipBinding,
            Some(target),
            format!(
                "SpiceDB {resource}.{rebac} is incompatible with the approved resource"
            ),
        ));
    }
    let approved =
        input.resources.get(&permission.target).ok_or_else(|| {
            Issue::new(
                IssueKind::InvalidCedarSchema,
                Some(target),
                "No approved Cedar resource projection",
            )
        })?;
    let action = cedar::action(
        &permission.target,
        approved,
        &cedar.schema,
        &input.principal,
        action,
    )?;
    let permission = RelationshipPermission::new(
        relationship.schema.version.clone(),
        relationship.bridge.version.clone(),
        resource,
        rebac.clone(),
    );
    let context =
        ContextPolicy::new(input.revision.version.clone(), action.to_string());
    Ok((AccessBinding::new(permission, context), action))
}

fn valid_relationship_name(name: &str) -> bool {
    (3..=64).contains(&name.len()) &&
        name.as_bytes()
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || *byte == b'_') &&
        name.as_bytes()
            .last()
            .is_some_and(u8::is_ascii_alphanumeric) &&
        name.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'
        })
}

struct RouteCompiler<'a> {
    project: &'a Project,
    environment: &'a SecurityEnvironment,
    cedar: &'a CompiledCedar,
    input: &'a CedarInput,
    propagation: &'a Propagation,
    bindings: &'a BTreeMap<PermissionRef, (AccessBinding, EntityUid)>,
}

impl RouteCompiler<'_> {
    fn descriptor(&self, route: &Route) -> Result<Descriptor, Issue> {
        self.validate_target(route)?;
        let dependency_routes =
            propagation::closure(route, &self.propagation.prerequisites);
        for dependency in &dependency_routes {
            if !self.environment.routes.contains_key(dependency) {
                return Err(Issue::new(
                    IssueKind::MissingRoute,
                    Some(dependency.target.clone()),
                    "A dependency requires its own approved access route",
                ));
            }
        }
        let resource =
            self.input.resources.get(route.owner()).ok_or_else(|| {
                Issue::new(
                    IssueKind::InvalidCedarSchema,
                    Some(route.target.clone()),
                    "The route has no Cedar resource projection",
                )
            })?;
        let names = self.permissions(route)?;
        let mut bindings = Vec::with_capacity(names.len());
        for name in &names {
            let key = PermissionRef {
                target: route.owner().clone(),
                name: name.clone(),
            };
            let (binding, action) =
                self.bindings.get(&key).ok_or_else(|| {
                    Issue::new(
                        IssueKind::InvalidTarget,
                        Some(route.target.clone()),
                        format!("The route names unknown permission {name}"),
                    )
                })?;
            let groups = cedar::groups(
                self.project,
                self.input,
                self.cedar,
                &route.target,
                name,
                action,
                resource,
            )?;
            bindings.push(Permission {
                name: name.clone(),
                binding: binding.clone(),
                action: action.clone(),
                groups,
            });
        }
        let flow = self
            .propagation
            .flows
            .get(&route.target)
            .cloned()
            .ok_or_else(|| {
                Issue::new(
                    IssueKind::InvalidFlow,
                    Some(route.target.clone()),
                    "The route has no effective IFC label",
                )
            })?;
        let mut control_flow = super::Flow::new(crate::Classification::Public);
        for name in &resource.fields {
            let field = FieldRef::new(route.owner().clone(), name.clone());
            let input = self
                .propagation
                .flows
                .get(&Reference::Field(field))
                .ok_or_else(|| {
                Issue::new(
                    IssueKind::InvalidFlow,
                    Some(route.target.clone()),
                    "A policy attribute has no effective IFC label",
                )
            })?;
            control_flow = control_flow.join(input);
        }
        Ok(Descriptor {
            resource_type: resource.entity_type.clone(),
            bindings,
            flow,
            dependencies: dependency_routes,
            fields: resource
                .fields
                .iter()
                .map(|name| FieldRef::new(route.owner().clone(), name.clone()))
                .collect(),
            context: self.input.context.keys().cloned().collect(),
            control_flow,
            compound: matches!(&route.target, Reference::Field(field) if propagation::structured_value(self.project, field)),
        })
    }

    fn permissions(&self, route: &Route) -> Result<BTreeSet<Name>, Issue> {
        let mut names =
            self.environment.routes.get(route).cloned().ok_or_else(|| {
                Issue::new(
                    IssueKind::MissingRoute,
                    Some(route.target.clone()),
                    "Missing an approved route for this operation",
                )
            })?;
        if let Some(parent) = inherited(route) {
            let inherited = self.environment.routes.get(&parent).filter(|permissions| !permissions.is_empty()).ok_or_else(|| Issue::new(
                IssueKind::MissingRoute, Some(parent.target), "The owner requires a route for this field's operation",
            ))?;
            names.extend(inherited.iter().cloned());
        }
        if let Some(security) = self.project.security().get(&route.target) {
            names.extend(
                security
                    .requires
                    .iter()
                    .map(|permission| permission.name.clone()),
            );
        }
        if names.is_empty() {
            return Err(Issue::new(
                IssueKind::MissingBinding,
                Some(route.target.clone()),
                "A route requires at least one mandatory permission",
            ));
        }
        if matches!(
            route.operation,
            Operation::Transfer | Operation::DescribeTransfer
        ) {
            let reads = self.permissions(&Route::new(
                route.target.clone(),
                if route.operation == Operation::DescribeTransfer {
                    Operation::Describe
                } else {
                    Operation::Read
                },
            ))?;
            // A new local spelling is insufficient if it resolves to the
            // same authority checks. Require a distinct external grant and
            // contextual action in addition to the original read obligations.
            if !names.iter().any(|name| {
                self.transfer_binding_is_distinct(route, name, &reads)
            }) {
                return Err(Issue::new(
                    IssueKind::InvalidTarget,
                    Some(route.target.clone()),
                    "Transfer requires a distinct relationship permission and Cedar action",
                ));
            }
            names.extend(reads);
        }
        Ok(names)
    }

    fn transfer_binding_is_distinct(
        &self,
        route: &Route,
        name: &Name,
        reads: &BTreeSet<Name>,
    ) -> bool {
        let binding = |name: &Name| {
            self.bindings.get(&PermissionRef {
                target: route.owner().clone(),
                name: name.clone(),
            })
        };
        let Some((transfer, action)) = binding(name) else {
            return false;
        };
        reads.iter().all(|name| {
            binding(name).is_some_and(|(read, read_action)| {
                transfer.relationship.permission !=
                    read.relationship.permission &&
                    action != read_action
            })
        })
    }

    fn validate_target(&self, route: &Route) -> Result<(), Issue> {
        if let Reference::Field(field) = &route.target {
            if matches!(route.operation, Operation::Read | Operation::Transfer) &&
                propagation::structured_value(self.project, field)
            {
                return Err(Issue::new(
                    IssueKind::InvalidFlow,
                    Some(route.target.clone()),
                    "Whole structured values require child-level enforcement",
                ));
            }
            if route.operation == Operation::Write &&
                self.propagation.derived.contains(field)
            {
                return Err(Issue::new(
                    IssueKind::InvalidTarget,
                    Some(route.target.clone()),
                    "A derived field cannot be written directly",
                ));
            }
        }
        let definition = declaration(self.project, route.owner());
        let allowed =
            match (&route.target, route.operation, definition) {
                (
                    Reference::Symbol(_),
                    Operation::Execute,
                    Some(Declaration::Action(_)),
                ) => true,
                (_, Operation::Read | Operation::Transfer, _) => {
                    self.propagation.flows.contains_key(&route.target)
                },
                (_, Operation::Describe | Operation::DescribeTransfer, _) => {
                    self.propagation.flows.contains_key(&route.target)
                },
                (
                    Reference::Symbol(_),
                    Operation::Write,
                    Some(Declaration::Entity(_) | Declaration::Relation(_)),
                ) => true,
                (
                    Reference::Field(field),
                    Operation::Write,
                    Some(Declaration::Entity(entity)),
                ) => entity.fields.get(field.name.as_str()).is_some_and(
                    |value| matches!(value.kind, FieldKind::Value(_)),
                ),
                (
                    Reference::Field(field),
                    Operation::Write,
                    Some(Declaration::Relation(relation)),
                ) => relation.fields.get(field.name.as_str()).is_some_and(
                    |value| matches!(value.kind, FieldKind::Value(_)),
                ),
                _ => false,
            };
        if allowed {
            Ok(())
        } else {
            Err(Issue::new(
                IssueKind::InvalidTarget,
                Some(route.target.clone()),
                "The selected target cannot perform this operation",
            ))
        }
    }
}
