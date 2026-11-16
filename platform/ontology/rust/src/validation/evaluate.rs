//! Evaluate only the checked declarative subset against admitted scalar facts.

use super::{Bindings, Budget, Error, Scalar, scalar};
use crate::checked::{Checked, Value};

pub(super) fn evaluate<'a>(
    expression: &'a Checked,
    bindings: &'a impl Bindings,
    budget: &mut Budget,
    depth: usize,
) -> Result<Scalar<'a>, Error> {
    if depth > budget.limits.max_depth {
        return Err(Error::Budget);
    }
    budget.step(1)?;
    let value = match &expression.value {
        Value::Literal(value) => scalar::literal(&expression.ty, value)?,
        Value::Context(name) => bindings.context(name, &expression.ty)?,
        Value::Variant(owner, name) => Scalar::Enum {
            owner,
            variant: name.as_str(),
        },
        Value::Field { base, path } => {
            bindings.field(base.as_deref(), path, &expression.ty)?
        },
        Value::Unary(operators, operand) => {
            let mut value = evaluate(operand, bindings, budget, depth + 1)?;
            for operator in operators.iter().rev() {
                budget.step(1)?;
                value = scalar::unary(*operator, value)?;
            }
            value
        },
        Value::Binary(first, rest) => {
            let mut value = evaluate(first, bindings, budget, depth + 1)?;
            for (operator, right) in rest {
                budget.step(1)?;
                let right = evaluate(right, bindings, budget, depth + 1)?;
                value = scalar::binary(value, *operator, right, budget)?;
            }
            value
        },
        Value::Parameter(_) | Value::Result | Value::Role { .. } => {
            return Err(Error::Unsupported);
        },
    };
    value.checked(&expression.ty)
}
