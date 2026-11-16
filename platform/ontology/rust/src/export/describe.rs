//! Release only metadata whose declaration, member and named types are
//! allowed.

use std::collections::BTreeSet;

use super::model::{Definition, FieldKind};
use super::source::Source;
use super::{ExportError, Projection, Selection, access_for, is_description};
use crate::authorization::{PermissionProvider, TrustedRuntime};
use crate::{
    Cardinality, FieldRef, Name, Reference, Role, SymbolKind, SymbolRef,
    TypeRef,
};

/// A member released after its own metadata obligations were checked.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PropertyDescription {
    /// Local name, which is itself protected metadata.
    pub name: Name,
    /// Canonical value or attribute ownership contract.
    pub kind: crate::FieldKind,
}

/// A relation participant whose target metadata is independently authorized.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoleDescription {
    /// Role name within the authorized relation.
    pub name: Name,
    /// Exact participant type and cardinality.
    pub role: Role,
}

/// A request-scoped description, independent of source or archive encoding.
///
/// Missing and denied members are indistinguishable. This description grants
/// no right to inspect values, execute actions, or renew a retired generation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolDescription {
    /// Exact declaration or field requested by the caller.
    pub target: Reference,
    /// Category of the owning declaration.
    pub kind: SymbolKind,
    /// Scalar, alias, field, or action result type when authorized.
    pub value_type: Option<TypeRef>,
    /// Independently authorized members, sorted by name.
    pub properties: Vec<PropertyDescription>,
    /// Authorized relation participants, sorted by role name.
    pub roles: Vec<RoleDescription>,
    /// Enumeration members covered by the declaration's metadata contract.
    pub variants: BTreeSet<Name>,
}

impl<R: crate::archive::RangeReader + Sync> crate::archive::ArchiveReader<R> {
    /// Describes a permitted definition without exposing raw compiler records.
    ///
    /// An empty field selection discovers all individually authorized members.
    /// Named value and role types require explicit `projection.definitions`
    /// approvals bound to real instances; the reader never invents them.
    pub async fn describe(
        &self,
        projection: &Projection,
        runtime: &(impl TrustedRuntime + Sync),
        provider: &(impl PermissionProvider + Sync),
    ) -> Option<SymbolDescription> {
        match describe(self, projection, runtime, provider).await {
            Ok(value) => Some(value),
            Err(_) => {
                tracing::debug!(event.name = "gol.description.unavailable");
                None
            },
        }
    }
}

async fn describe(
    source: &impl Source,
    projection: &Projection,
    runtime: &(impl TrustedRuntime + Sync),
    provider: &(impl PermissionProvider + Sync),
) -> Result<SymbolDescription, ExportError> {
    if projection.definitions.len() > 1024 ||
        projection.root.fields.len() > 1024 ||
        !approved(source, projection, &projection.root, runtime, provider)
            .await?
    {
        return Err(ExportError::Unavailable);
    }
    let target = &projection.root.access.route.target;
    let owner = match target {
        Reference::Symbol(symbol) => symbol,
        Reference::Field(field) => &field.owner,
    };
    let definition = source.definition(owner).await?;
    let (kind, fields, roles, variants, value_type, parameters) =
        match definition {
            Definition::Entity { fields } => (
                SymbolKind::Entity,
                fields,
                Default::default(),
                Default::default(),
                None,
                false,
            ),
            Definition::Struct { fields } => (
                SymbolKind::Struct,
                fields,
                Default::default(),
                Default::default(),
                None,
                false,
            ),
            Definition::Relation { fields, roles } => (
                SymbolKind::Relation,
                fields,
                roles,
                Default::default(),
                None,
                false,
            ),
            Definition::Enum { variants } => (
                SymbolKind::Enum,
                Default::default(),
                Default::default(),
                variants,
                None,
                false,
            ),
            Definition::TypeAlias { target } => (
                SymbolKind::TypeAlias,
                Default::default(),
                Default::default(),
                Default::default(),
                Some(target),
                false,
            ),
            Definition::Attribute { value_type } => (
                SymbolKind::Attribute,
                Default::default(),
                Default::default(),
                Default::default(),
                Some(value_type),
                false,
            ),
            Definition::Action { parameters, result } => (
                SymbolKind::Action,
                parameters
                    .into_iter()
                    .map(|(name, ty)| {
                        (
                            name,
                            super::model::Field {
                                kind: FieldKind::Value(ty),
                            },
                        )
                    })
                    .collect(),
                Default::default(),
                Default::default(),
                Some(result),
                true,
            ),
            Definition::Unsupported => return Err(ExportError::Unsupported),
        };
    if fields.len() > 1024 || roles.len() > 1024 || variants.len() > 1024 {
        return Err(ExportError::Limit);
    }
    let mut description = SymbolDescription {
        target: target.clone(),
        kind,
        value_type: None,
        properties: Vec::new(),
        roles: Vec::new(),
        variants,
    };
    if let Some(ty) = value_type &&
        type_allowed(source, projection, &ty, runtime, provider).await?
    {
        description.value_type = Some(ty);
    }
    for (name, field) in fields {
        let selected = match target {
            Reference::Field(field) => field.name == name,
            Reference::Symbol(_) => {
                projection.root.fields.is_empty() ||
                    projection.root.fields.contains(&name)
            },
        };
        if !selected {
            continue;
        }
        if !parameters {
            let access = access_for(
                &projection.root.access,
                FieldRef::new(owner.clone(), name.clone()).into(),
                projection.root.access.route.operation,
            );
            if !source.permitted(&access, runtime, provider).await? {
                continue;
            }
        }
        let ty = match &field.kind {
            FieldKind::Key(ty) | FieldKind::Value(ty) => ty.clone(),
            FieldKind::OwnedAttribute { attribute, .. } => {
                TypeRef::Named(attribute.clone())
            },
        };
        if !type_allowed(source, projection, &ty, runtime, provider).await? {
            continue;
        }
        let kind = match field.kind {
            FieldKind::Key(ty) => crate::FieldKind::Key(ty),
            FieldKind::Value(ty) => crate::FieldKind::Value(ty),
            FieldKind::OwnedAttribute {
                attribute,
                cardinality,
            } => crate::FieldKind::OwnedAttribute {
                attribute,
                cardinality: Cardinality::new(
                    cardinality.min,
                    cardinality.max,
                )
                .map_err(|_| ExportError::Unavailable)?,
            },
        };
        if matches!(target, Reference::Field(_)) {
            description.value_type = Some(ty);
        }
        description
            .properties
            .push(PropertyDescription { name, kind });
    }
    if matches!(target, Reference::Symbol(_)) {
        for (name, role) in roles {
            if type_allowed(
                source,
                projection,
                &TypeRef::Entity(role.entity.clone()),
                runtime,
                provider,
            )
            .await?
            {
                description.roles.push(RoleDescription {
                    name,
                    role: Role::new(
                        role.entity,
                        Cardinality::new(
                            role.cardinality.min,
                            role.cardinality.max,
                        )
                        .map_err(|_| ExportError::Unavailable)?,
                    ),
                });
            }
        }
    }
    Ok(description)
}

async fn type_allowed(
    source: &impl Source,
    projection: &Projection,
    mut ty: &TypeRef,
    runtime: &(impl TrustedRuntime + Sync),
    provider: &(impl PermissionProvider + Sync),
) -> Result<bool, ExportError> {
    // Type expressions contain one nested element, so bounded iteration avoids
    // recursive futures and a heap allocation for every optional/list wrapper.
    for _ in 0..32 {
        match ty {
            TypeRef::Option(inner) | TypeRef::List { element: inner, .. } => {
                ty = inner
            },
            TypeRef::Named(symbol) | TypeRef::Entity(symbol) => {
                let Some(selection) = projection.definitions.get(symbol)
                else {
                    return Ok(false);
                };
                if selection.access.route.target !=
                    Reference::Symbol(symbol.clone())
                {
                    return Ok(false);
                }
                return approved(
                    source, projection, selection, runtime, provider,
                )
                .await;
            },
            TypeRef::Primitive(_) | TypeRef::External(_) => return Ok(true),
        }
    }
    Err(ExportError::Limit)
}

async fn approved(
    source: &impl Source,
    projection: &Projection,
    selection: &Selection,
    runtime: &(impl TrustedRuntime + Sync),
    provider: &(impl PermissionProvider + Sync),
) -> Result<bool, ExportError> {
    let root = &projection.root.access;
    let access = &selection.access;
    let owner: &SymbolRef = match &access.route.target {
        Reference::Symbol(symbol) => symbol,
        Reference::Field(field) => &field.owner,
    };
    if !is_description(access.route.operation) ||
        root.route.operation != access.route.operation ||
        root.instance.tenant != access.instance.tenant ||
        root.destination != access.destination ||
        root.purpose != access.purpose ||
        owner != &access.instance.owner
    {
        return Err(ExportError::Unavailable);
    }
    source.permitted(access, runtime, provider).await
}
