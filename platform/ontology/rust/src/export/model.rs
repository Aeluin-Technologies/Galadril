//! Decode the stable canonical payload without depending on the GOL parser.

use std::collections::{BTreeMap, BTreeSet};

use serde::Deserialize;

use super::ExportError;
use crate::{Name, SymbolRef, TypeRef};

#[derive(Deserialize)]
pub(super) enum Definition {
    Entity {
        fields: BTreeMap<Name, Field>,
    },
    Struct {
        fields: BTreeMap<Name, Field>,
    },
    Relation {
        fields: BTreeMap<Name, Field>,
        roles: BTreeMap<Name, Role>,
    },
    Enum {
        variants: BTreeSet<Name>,
    },
    TypeAlias {
        target: TypeRef,
    },
    Attribute {
        value_type: TypeRef,
    },
    Action {
        parameters: BTreeMap<Name, TypeRef>,
        result: TypeRef,
    },
    #[serde(other)]
    Unsupported,
}

#[derive(Deserialize)]
pub(super) struct Field {
    pub kind: FieldKind,
}

#[derive(Deserialize)]
pub(super) enum FieldKind {
    Key(TypeRef),
    Value(TypeRef),
    OwnedAttribute {
        attribute: SymbolRef,
        cardinality: Bounds,
    },
}

#[derive(Deserialize)]
pub(super) struct Bounds {
    pub min: u32,
    pub max: Option<u32>,
}

#[derive(Deserialize)]
pub(super) struct Role {
    pub entity: SymbolRef,
    pub cardinality: Bounds,
}

impl FieldKind {
    pub fn value_type(self) -> Result<TypeRef, ExportError> {
        match self {
            Self::Key(ty) | Self::Value(ty) => Ok(ty),
            Self::OwnedAttribute { cardinality, .. } => {
                if cardinality.max.is_some_and(|max| max < cardinality.min) {
                    return Err(ExportError::Unavailable);
                }
                // Attribute multiplicity needs a value-to-array adapter,
                // which cannot be inferred from its nominal type.
                Err(ExportError::Unsupported)
            },
        }
    }
}

pub(super) fn canonical_definition(
    payload: &[u8],
) -> Result<Definition, ExportError> {
    type Record = (
        serde::de::IgnoredAny,
        Definition,
        serde::de::IgnoredAny,
        serde::de::IgnoredAny,
    );
    let (_, definition, _, _) = crate::archive::decode::<Record>(payload)
        .map_err(|_| ExportError::Unavailable)?;
    Ok(definition)
}

pub(super) fn description_inventory(
    payload: &[u8],
) -> Result<(BTreeSet<Name>, BTreeMap<SymbolRef, bool>), ExportError> {
    let definition = canonical_definition(payload)?;
    let mut names = BTreeSet::new();
    let mut types = Vec::new();
    let mut references = BTreeMap::new();
    if let Definition::Relation { roles, .. } = &definition {
        references
            .extend(roles.values().map(|role| (role.entity.clone(), false)));
    }
    match definition {
        Definition::Entity { fields } |
        Definition::Struct { fields } |
        Definition::Relation { fields, .. } => {
            for (name, field) in fields {
                names.insert(name);
                match field.kind {
                    FieldKind::Key(ty) | FieldKind::Value(ty) => {
                        types.push(ty)
                    },
                    FieldKind::OwnedAttribute { attribute, .. } => {
                        references.insert(attribute, true);
                    },
                }
            }
        },
        Definition::TypeAlias { target } |
        Definition::Attribute { value_type: target } => types.push(target),
        Definition::Action { parameters, result } => {
            types.extend(parameters.into_values());
            types.push(result);
        },
        _ => {},
    }
    for mut ty in types {
        for _ in 0..32 {
            match ty {
                TypeRef::Option(inner) |
                TypeRef::List { element: inner, .. } => ty = *inner,
                TypeRef::Named(symbol) => {
                    references.insert(symbol, true);
                    break;
                },
                TypeRef::Entity(symbol) => {
                    references.entry(symbol).or_insert(false);
                    break;
                },
                _ => break,
            }
        }
    }
    Ok((names, references))
}
