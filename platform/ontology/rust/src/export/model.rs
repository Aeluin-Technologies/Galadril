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
        #[serde(rename = "attribute")]
        _attribute: SymbolRef,
        cardinality: Bounds,
    },
}

#[derive(Deserialize)]
pub(super) struct Bounds {
    pub min: u32,
    pub max: Option<u32>,
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
