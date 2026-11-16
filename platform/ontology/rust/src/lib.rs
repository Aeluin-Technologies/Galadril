//! Canonical, in-memory schema and contracts for Galadril Ontology Language.
//!
//! Use a module version to pin references, including references to
//! declarations that will be inserted later. This lets mutually referential
//! business types share the same logical scope.
//!
//! ```
//! use galadril_ontology::{
//!     Declaration, Entity, Field, FieldKind, ModuleVersion, Named, Ontology,
//!     SymbolKind, TypeRef, Version,
//! };
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let module =
//!     ModuleVersion::new("industry.assets".parse()?, Version::new(1, 0, 0));
//! let machine = module.reference("Machine".parse()?);
//! let mut ontology = Ontology::new(module);
//! let mut fields = Named::new();
//! fields.insert(
//!     "replacement".parse()?,
//!     Field::new(FieldKind::Value(TypeRef::Option(Box::new(
//!         TypeRef::Entity(machine.clone()),
//!     )))),
//! )?;
//! ontology.declare(
//!     "Machine".parse()?,
//!     Declaration::Entity(Entity {
//!         fields,
//!         ..Entity::default()
//!     }),
//! )?;
//! assert_eq!(
//!     ontology.resolve_local(&machine).map(Declaration::kind),
//!     Some(SymbolKind::Entity)
//! );
//! # Ok(())
//! # }
//! ```

#![forbid(unsafe_code)]
#![deny(missing_docs)]

mod declaration;
mod error;
mod expression;
mod external_type;
mod identity;
mod named;
mod ontology;
mod security;
mod value_type;

#[cfg(feature = "parser")]
pub mod semantic;

#[cfg(feature = "parser")]
pub mod syntax;

pub use declaration::{Declaration, Entity, Relation, Role};
pub use error::ModelError;
pub use expression::{
    Action, Derivation, Effect, Expression, FieldRef, Idempotence, Invariant,
    Predicate, Reference, TypedExpression, View,
};
pub use external_type::{
    AdapterContract, ExternalSchema, ExternalTypeDescriptor, ExternalTypeId,
    ExternalTypeRegistry, TypeCapability,
};
pub use identity::{
    ModuleVersion, Name, OntologyId, OntologyVersion, QualifiedName, SymbolId,
    SymbolKey, SymbolKind, SymbolRef, VersionedName,
};
pub use named::Named;
pub use ontology::{Import, ImportSelection, Ontology};
pub use security::{
    AccessBinding, Classification, ContextPolicy, FlowContract, IfcLabel,
    RelationshipPermission, SecurityContract, TransferConstraint,
};
pub use semver::{Version, VersionReq};
pub use value_type::{
    Attribute, Cardinality, EnumType, Field, FieldKind, Primitive, StructType,
    TypeAlias, TypeRef,
};
