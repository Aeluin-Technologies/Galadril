//! Host-approved inputs for offline security compilation.

use std::collections::{BTreeMap, BTreeSet};

use cedar_policy::{EntityTypeName, EntityUid};

use super::contract::{AuthorityRevision, Route};
use super::flow::{Flow, Sink};
use crate::{
    ExternalTypeId, Name, OntologyId, Primitive, QualifiedName, SymbolRef,
};

/// An approved snapshot of the relevant SpiceDB schema and ID bridge.
#[non_exhaustive]
pub struct RelationshipInput {
    /// Immutable schema version supplied by the authority.
    pub schema: AuthorityRevision,
    /// Version of the GOL instance-to-object mapping.
    pub bridge: AuthorityRevision,
    /// Domain types and their approved SpiceDB resource definitions.
    pub resources: BTreeMap<SymbolRef, RelationshipResource>,
}

impl RelationshipInput {
    /// Creates a version-pinned schema input with no resource mappings.
    #[must_use]
    pub fn new(schema: AuthorityRevision, bridge: AuthorityRevision) -> Self {
        Self {
            schema,
            bridge,
            resources: BTreeMap::new(),
        }
    }
}

/// The approved projection of one SpiceDB resource definition.
#[non_exhaustive]
pub struct RelationshipResource {
    /// Exact resource type in the external schema.
    pub resource_type: Box<str>,
    /// Actual permission names from that resource definition.
    pub permissions: BTreeSet<Name>,
}

impl RelationshipResource {
    /// Binds a domain resource to the approved permissions on an object type.
    #[must_use]
    pub fn new(
        resource_type: impl Into<Box<str>>,
        permissions: BTreeSet<Name>,
    ) -> Self {
        Self {
            resource_type: resource_type.into(),
            permissions,
        }
    }
}

/// A versioned Cedar policy bundle and its trusted schema inputs.
#[non_exhaustive]
pub struct CedarInput {
    /// Immutable version covering the policy and schema fragments.
    pub revision: AuthorityRevision,
    /// The externally authenticated principal type.
    pub principal: EntityTypeName,
    /// Cedar schema fragments from trusted authorities, such as IAM.
    pub schemas: Vec<Box<str>>,
    /// Native Cedar policy text from the approved bundle.
    pub policies: Box<str>,
    /// Required trusted context keys and their supported GOL types.
    pub context: BTreeMap<Name, Primitive>,
    /// Approved projections and actions keyed by exact GOL resource type.
    pub resources: BTreeMap<SymbolRef, CedarResource>,
}

impl CedarInput {
    /// Creates a bundle before adding entity and action projections.
    #[must_use]
    pub fn new(
        revision: AuthorityRevision,
        principal: EntityTypeName,
        policies: impl Into<Box<str>>,
    ) -> Self {
        Self {
            revision,
            principal,
            policies: policies.into(),
            schemas: Vec::new(),
            context: BTreeMap::new(),
            resources: BTreeMap::new(),
        }
    }
}

/// The approved Cedar projection of a GOL resource and its actions.
#[non_exhaustive]
pub struct CedarResource {
    /// The Cedar entity type to generate from the projection.
    pub entity_type: EntityTypeName,
    /// Explicitly exposed GOL properties.
    pub fields: BTreeSet<Name>,
    /// Exact Cedar action UIDs applicable to this resource.
    pub actions: BTreeSet<EntityUid>,
}

impl CedarResource {
    /// Starts with a resource identity and no exposed attributes or actions.
    #[must_use]
    pub fn new(entity_type: EntityTypeName) -> Self {
        Self {
            entity_type,
            fields: BTreeSet::new(),
            actions: BTreeSet::new(),
        }
    }
}

/// Inputs assembled by the host's trusted configuration boundary.
#[non_exhaustive]
pub struct SecurityEnvironment {
    /// Immutable revision of the route and sink configuration.
    pub revision: AuthorityRevision,
    /// Approved SpiceDB schema metadata.
    pub relationship: Option<RelationshipInput>,
    /// Approved Cedar schemas and policies.
    pub cedar: Option<CedarInput>,
    /// Operations the host exposes, with their required local permission
    /// names.
    pub routes: BTreeMap<Route, BTreeSet<Name>>,
    /// Explicit deployment clearance floor for every GOL value.
    pub baseline: Flow,
    /// Additional floors inherited by each module's resources.
    pub module_floors: BTreeMap<OntologyId, Flow>,
    /// Independently approved receiving destinations.
    pub sinks: BTreeMap<QualifiedName, Sink>,
    /// Exact adapter descriptors approved by the external type authority.
    pub approved_types: BTreeSet<ExternalTypeId>,
}

impl SecurityEnvironment {
    /// Starts an offline compilation with an explicit IFC floor and revision.
    #[must_use]
    pub fn new(revision: AuthorityRevision, baseline: Flow) -> Self {
        Self {
            revision,
            relationship: None,
            cedar: None,
            routes: BTreeMap::new(),
            baseline,
            module_floors: BTreeMap::new(),
            sinks: BTreeMap::new(),
            approved_types: BTreeSet::new(),
        }
    }
}
