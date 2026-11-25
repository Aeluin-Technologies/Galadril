//! Exact Rust domains prevent serialization from inventing capabilities.

use std::cmp::Ordering;

use super::{Budget, Error};
use crate::checked::{BinaryOp, Literal, UnaryOp};
use crate::{Primitive, SymbolRef, TypeRef};

/// A borrowed, already admitted scalar with an exact GOL value domain.
///
/// Enum membership and field ownership are established by the host's shape
/// validator; this kernel retains nominal identity and checks type equality.
#[derive(Debug, Clone, Copy, PartialEq)]
#[non_exhaustive]
pub enum Scalar<'a> {
    /// A Boolean.
    Bool(bool),
    /// A Unicode scalar.
    Char(char),
    /// Borrowed UTF-8 text.
    String(&'a str),
    /// A signed 8-bit value.
    I8(i8),
    /// A signed 16-bit value.
    I16(i16),
    /// A signed 32-bit value.
    I32(i32),
    /// A signed 64-bit value.
    I64(i64),
    /// A signed 128-bit value.
    I128(i128),
    /// An unsigned 8-bit value.
    U8(u8),
    /// An unsigned 16-bit value.
    U16(u16),
    /// An unsigned 32-bit value.
    U32(u32),
    /// An unsigned 64-bit value.
    U64(u64),
    /// An unsigned 128-bit value.
    U128(u128),
    /// A finite binary32 value.
    F32(f32),
    /// A finite binary64 value.
    F64(f64),
    /// A variant in an exact version-pinned enum domain.
    Enum {
        /// The checked enum identity.
        owner: &'a SymbolRef,
        /// The host-validated variant name.
        variant: &'a str,
    },
}

impl Scalar<'_> {
    pub(super) fn checked(self, ty: &TypeRef) -> Result<Self, Error> {
        let matches = match (ty, self) {
            (TypeRef::Primitive(expected), actual) => {
                actual.primitive() == Some(*expected)
            },
            (TypeRef::Named(expected), Self::Enum { owner, .. }) => {
                owner == expected
            },
            (
                TypeRef::External(_) |
                TypeRef::Entity(_) |
                TypeRef::Option(_) |
                TypeRef::List { .. },
                _,
            ) => return Err(Error::Unsupported),
            _ => false,
        };
        if !matches {
            return Err(Error::TypeMismatch);
        }
        match self {
            Self::F32(value) if !value.is_finite() => Err(Error::Arithmetic),
            Self::F64(value) if !value.is_finite() => Err(Error::Arithmetic),
            _ => Ok(self),
        }
    }

    fn primitive(self) -> Option<Primitive> {
        Some(match self {
            Self::Bool(_) => Primitive::Bool,
            Self::Char(_) => Primitive::Char,
            Self::String(_) => Primitive::String,
            Self::I8(_) => Primitive::I8,
            Self::I16(_) => Primitive::I16,
            Self::I32(_) => Primitive::I32,
            Self::I64(_) => Primitive::I64,
            Self::I128(_) => Primitive::I128,
            Self::U8(_) => Primitive::U8,
            Self::U16(_) => Primitive::U16,
            Self::U32(_) => Primitive::U32,
            Self::U64(_) => Primitive::U64,
            Self::U128(_) => Primitive::U128,
            Self::F32(_) => Primitive::F32,
            Self::F64(_) => Primitive::F64,
            Self::Enum { .. } => return None,
        })
    }

    fn signed(self) -> Option<(Primitive, i128)> {
        Some(match self {
            Self::I8(value) => (Primitive::I8, i128::from(value)),
            Self::I16(value) => (Primitive::I16, i128::from(value)),
            Self::I32(value) => (Primitive::I32, i128::from(value)),
            Self::I64(value) => (Primitive::I64, i128::from(value)),
            Self::I128(value) => (Primitive::I128, value),
            _ => return None,
        })
    }

    fn unsigned(self) -> Option<(Primitive, u128)> {
        Some(match self {
            Self::U8(value) => (Primitive::U8, u128::from(value)),
            Self::U16(value) => (Primitive::U16, u128::from(value)),
            Self::U32(value) => (Primitive::U32, u128::from(value)),
            Self::U64(value) => (Primitive::U64, u128::from(value)),
            Self::U128(value) => (Primitive::U128, value),
            _ => return None,
        })
    }
}

fn signed(ty: Primitive, value: i128) -> Result<Scalar<'static>, Error> {
    Ok(match ty {
        Primitive::I8 => {
            Scalar::I8(i8::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::I16 => {
            Scalar::I16(i16::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::I32 => {
            Scalar::I32(i32::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::I64 => {
            Scalar::I64(i64::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::I128 => Scalar::I128(value),
        _ => return Err(Error::TypeMismatch),
    })
}
fn unsigned(ty: Primitive, value: u128) -> Result<Scalar<'static>, Error> {
    Ok(match ty {
        Primitive::U8 => {
            Scalar::U8(u8::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::U16 => {
            Scalar::U16(u16::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::U32 => {
            Scalar::U32(u32::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::U64 => {
            Scalar::U64(u64::try_from(value).map_err(|_| Error::Arithmetic)?)
        },
        Primitive::U128 => Scalar::U128(value),
        _ => return Err(Error::TypeMismatch),
    })
}

pub(super) fn literal<'a>(
    ty: &TypeRef,
    value: &'a Literal,
) -> Result<Scalar<'a>, Error> {
    let TypeRef::Primitive(primitive) = ty else {
        return Err(Error::Unsupported);
    };
    let scalar = match value {
        Literal::Bool(value) => Scalar::Bool(*value),
        Literal::Char(value) => Scalar::Char(*value),
        Literal::String(value) => Scalar::String(value),
        Literal::Integer(value) => match primitive {
            Primitive::I8 |
            Primitive::I16 |
            Primitive::I32 |
            Primitive::I64 |
            Primitive::I128 => signed(
                *primitive,
                i128::try_from(*value).map_err(|_| Error::Arithmetic)?,
            )?,
            _ => unsigned(*primitive, *value)?,
        },
        Literal::NegativeInteger(value) => {
            // The magnitude of i128::MIN is not representable as i128.
            let signed_value = if *value == 1_u128 << 127 {
                i128::MIN
            } else {
                i128::try_from(*value)
                    .ok()
                    .and_then(i128::checked_neg)
                    .ok_or(Error::Arithmetic)?
            };
            signed(*primitive, signed_value)?
        },
        Literal::Float(bits) => match primitive {
            Primitive::F32 => Scalar::F32(f32::from_bits(
                u32::try_from(*bits).map_err(|_| Error::Arithmetic)?,
            )),
            Primitive::F64 => Scalar::F64(f64::from_bits(*bits)),
            _ => return Err(Error::TypeMismatch),
        },
    };
    scalar.checked(ty)
}

pub(super) fn unary(
    operator: UnaryOp,
    value: Scalar<'_>,
) -> Result<Scalar<'_>, Error> {
    match (operator, value) {
        (UnaryOp::Not, Scalar::Bool(value)) => Ok(Scalar::Bool(!value)),
        (UnaryOp::Negate, Scalar::F32(value)) => Ok(Scalar::F32(-value)),
        (UnaryOp::Negate, Scalar::F64(value)) => Ok(Scalar::F64(-value)),
        (UnaryOp::Negate, value) => {
            let (ty, value) = value.signed().ok_or(Error::TypeMismatch)?;
            signed(ty, value.checked_neg().ok_or(Error::Arithmetic)?)
        },
        _ => Err(Error::TypeMismatch),
    }
}

pub(super) fn binary<'a>(
    left: Scalar<'a>,
    op: BinaryOp,
    right: Scalar<'a>,
    budget: &mut Budget,
) -> Result<Scalar<'a>, Error> {
    if left.primitive() != right.primitive() {
        return Err(Error::TypeMismatch);
    }
    match op {
        BinaryOp::And | BinaryOp::Or => match (left, right) {
            (Scalar::Bool(a), Scalar::Bool(b)) => {
                Ok(Scalar::Bool(if op == BinaryOp::And {
                    a && b
                } else {
                    a || b
                }))
            },
            _ => Err(Error::TypeMismatch),
        },
        BinaryOp::Equal | BinaryOp::NotEqual => {
            let same = equal(left, right, budget)?;
            Ok(Scalar::Bool(if op == BinaryOp::Equal {
                same
            } else {
                !same
            }))
        },
        BinaryOp::Less |
        BinaryOp::LessEqual |
        BinaryOp::Greater |
        BinaryOp::GreaterEqual => {
            let ordering = order(left, right, budget)?;
            Ok(Scalar::Bool(match op {
                BinaryOp::Less => ordering.is_lt(),
                BinaryOp::LessEqual => ordering.is_le(),
                BinaryOp::Greater => ordering.is_gt(),
                BinaryOp::GreaterEqual => ordering.is_ge(),
                _ => return Err(Error::Invalid),
            }))
        },
        _ => arithmetic(left, op, right),
    }
}

fn text_budget(
    left: &str,
    right: &str,
    budget: &mut Budget,
) -> Result<(), Error> {
    if left.len() > budget.limits.max_bytes ||
        right.len() > budget.limits.max_bytes
    {
        return Err(Error::Budget);
    }
    budget.step(1 + left.len().min(right.len()) / 16)
}
fn equal(
    left: Scalar<'_>,
    right: Scalar<'_>,
    budget: &mut Budget,
) -> Result<bool, Error> {
    match (left, right) {
        (Scalar::String(a), Scalar::String(b)) => {
            text_budget(a, b, budget)?;
            Ok(a == b)
        },
        (
            Scalar::Enum {
                owner: a,
                variant: av,
            },
            Scalar::Enum {
                owner: b,
                variant: bv,
            },
        ) => {
            if a != b {
                return Err(Error::TypeMismatch);
            }
            text_budget(av, bv, budget)?;
            Ok(av == bv)
        },
        _ => Ok(left == right),
    }
}
fn order(
    left: Scalar<'_>,
    right: Scalar<'_>,
    budget: &mut Budget,
) -> Result<Ordering, Error> {
    if let (Some((at, a)), Some((bt, b))) = (left.signed(), right.signed()) &&
        at == bt
    {
        return Ok(a.cmp(&b));
    }
    if let (Some((at, a)), Some((bt, b))) = (left.unsigned(), right.unsigned()) &&
        at == bt
    {
        return Ok(a.cmp(&b));
    }
    match (left, right) {
        (Scalar::String(a), Scalar::String(b)) => {
            text_budget(a, b, budget)?;
            Ok(a.cmp(b))
        },
        (Scalar::Char(a), Scalar::Char(b)) => Ok(a.cmp(&b)),
        _ => Err(Error::Unsupported),
    }
}

fn arithmetic<'a>(
    left: Scalar<'a>,
    op: BinaryOp,
    right: Scalar<'a>,
) -> Result<Scalar<'a>, Error> {
    if let (Some((at, a)), Some((bt, b))) = (left.signed(), right.signed()) &&
        at == bt
    {
        let value = match op {
            BinaryOp::Add => a.checked_add(b),
            BinaryOp::Subtract => a.checked_sub(b),
            BinaryOp::Multiply => a.checked_mul(b),
            BinaryOp::Divide => a.checked_div(b),
            BinaryOp::Remainder => a.checked_rem(b),
            _ => return Err(Error::Unsupported),
        }
        .ok_or(Error::Arithmetic)?;
        return signed(at, value);
    }
    if let (Some((at, a)), Some((bt, b))) = (left.unsigned(), right.unsigned()) &&
        at == bt
    {
        let value = match op {
            BinaryOp::Add => a.checked_add(b),
            BinaryOp::Subtract => a.checked_sub(b),
            BinaryOp::Multiply => a.checked_mul(b),
            BinaryOp::Divide => a.checked_div(b),
            BinaryOp::Remainder => a.checked_rem(b),
            _ => return Err(Error::Unsupported),
        }
        .ok_or(Error::Arithmetic)?;
        return unsigned(at, value);
    }
    match (left, right) {
        (Scalar::F32(a), Scalar::F32(b)) => {
            let value = match op {
                BinaryOp::Add => a + b,
                BinaryOp::Subtract => a - b,
                BinaryOp::Multiply => a * b,
                BinaryOp::Divide if b != 0.0 => a / b,
                _ => return Err(Error::Arithmetic),
            };
            if value.is_finite() {
                Ok(Scalar::F32(value))
            } else {
                Err(Error::Arithmetic)
            }
        },
        (Scalar::F64(a), Scalar::F64(b)) => {
            let value = match op {
                BinaryOp::Add => a + b,
                BinaryOp::Subtract => a - b,
                BinaryOp::Multiply => a * b,
                BinaryOp::Divide if b != 0.0 => a / b,
                _ => return Err(Error::Arithmetic),
            };
            if value.is_finite() {
                Ok(Scalar::F64(value))
            } else {
                Err(Error::Arithmetic)
            }
        },
        _ => Err(Error::Unsupported),
    }
}
