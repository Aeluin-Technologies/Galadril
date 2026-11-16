//! Check pure invariants only after the reader validates the complete JSON
//! shape.

use galadril_ontology::authorization::Access;
use galadril_ontology::checked::{Checked, Value as CheckedValue};
use galadril_ontology::export::{GolValidation, GolValidator};
use galadril_ontology::validation::{
    Bindings, Contract, Error, Limits, Scalar,
};
use galadril_ontology::{FieldRef, Primitive, TypeRef};
use serde_json::Value;

pub(super) struct Business<'a> {
    contract: Contract,
    value: &'a Value,
}

impl<'a> Business<'a> {
    pub fn new(contract: Contract, value: &'a Value) -> Self {
        Self { contract, value }
    }
}

impl Bindings for Business<'_> {
    fn field<'a>(
        &'a self,
        base: Option<&Checked>,
        path: &[FieldRef],
        expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        let [field] = path else {
            return Err(Error::Unsupported);
        };
        if base.is_some() || &field.owner != self.contract.owner() {
            return Err(Error::Unsupported);
        }
        let value = self
            .value
            .get(field.name.as_str())
            .ok_or(Error::MissingBinding)?;
        let text = || value.as_str().ok_or(Error::TypeMismatch);
        let signed = || match value {
            Value::String(text) => {
                text.parse::<i128>().map_err(|_| Error::TypeMismatch)
            },
            _ => value.as_i64().map(i128::from).ok_or(Error::TypeMismatch),
        };
        let unsigned = || match value {
            Value::String(text) => {
                text.parse::<u128>().map_err(|_| Error::TypeMismatch)
            },
            _ => value.as_u64().map(u128::from).ok_or(Error::TypeMismatch),
        };
        Ok(match expected {
            TypeRef::Primitive(primitive) => match primitive {
                Primitive::Bool => {
                    Scalar::Bool(value.as_bool().ok_or(Error::TypeMismatch)?)
                },
                Primitive::String => Scalar::String(text()?),
                Primitive::Char => {
                    let mut chars = text()?.chars();
                    let first = chars.next().ok_or(Error::TypeMismatch)?;
                    if chars.next().is_some() {
                        return Err(Error::TypeMismatch);
                    }
                    Scalar::Char(first)
                },
                Primitive::I8 => Scalar::I8(
                    signed()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::I16 => Scalar::I16(
                    signed()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::I32 => Scalar::I32(
                    signed()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::I64 => Scalar::I64(
                    signed()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::I128 => Scalar::I128(signed()?),
                Primitive::U8 => Scalar::U8(
                    unsigned()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::U16 => Scalar::U16(
                    unsigned()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::U32 => Scalar::U32(
                    unsigned()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::U64 => Scalar::U64(
                    unsigned()?.try_into().map_err(|_| Error::TypeMismatch)?,
                ),
                Primitive::U128 => Scalar::U128(unsigned()?),
                Primitive::F32 => Scalar::F32(
                    value.as_f64().ok_or(Error::TypeMismatch)? as f32,
                ),
                Primitive::F64 => {
                    Scalar::F64(value.as_f64().ok_or(Error::TypeMismatch)?)
                },
                _ => return Err(Error::Unsupported),
            },
            TypeRef::Named(expected) => {
                let owner = self
                    .contract
                    .predicates()
                    .iter()
                    .find_map(|predicate| {
                        enum_owner(predicate.body(), expected)
                    })
                    .ok_or(Error::Unsupported)?;
                Scalar::Enum {
                    owner,
                    variant: text()?,
                }
            },
            _ => return Err(Error::Unsupported),
        })
    }
}

fn enum_owner<'a>(
    checked: &'a Checked,
    expected: &galadril_ontology::SymbolRef,
) -> Option<&'a galadril_ontology::SymbolRef> {
    if let TypeRef::Named(owner) = &checked.ty &&
        owner == expected
    {
        return Some(owner);
    }
    match &checked.value {
        CheckedValue::Unary(_, inner) => enum_owner(inner, expected),
        CheckedValue::Binary(first, rest) => enum_owner(first, expected)
            .or_else(|| {
                rest.iter()
                    .find_map(|(_, value)| enum_owner(value, expected))
            }),
        CheckedValue::Field {
            base: Some(base), ..
        } => enum_owner(base, expected),
        _ => None,
    }
}

impl GolValidator for Business<'_> {
    async fn validate(
        &self,
        target: &Access,
        proposal: &Value,
    ) -> GolValidation {
        if super::owner(&target.route.target)
            .is_ok_and(|owner| owner == self.contract.owner()) &&
            proposal == self.value &&
            self.contract.check(self, Limits::default()).is_ok()
        {
            GolValidation::Accepted
        } else {
            GolValidation::Rejected
        }
    }
}
