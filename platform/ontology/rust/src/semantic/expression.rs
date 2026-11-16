//! Type checking for declarative expressions and action-relative paths.

use std::collections::BTreeSet;

use super::diagnostic::{CheckedResult, Code, Diagnostic};
use super::index::{Scope, Target, spelling};
use super::ir::{Checked, Environment, Literal, Value};
use super::types::{Facts, compatible, field_type, named_fields, peel};
use crate::syntax::{self as ast, BinaryOp, Expression, UnaryOp};
use crate::{
    Action, Declaration, FieldRef, Primitive, Reference, SymbolRef,
    TypeCapability, TypeRef,
};

pub(super) struct Checker<'a, 'source> {
    pub scope: &'a Scope<'source>,
    pub facts: &'a Facts,
    pub environment: &'a Environment<'a>,
    pub subject: Option<&'a SymbolRef>,
    pub action: Option<&'a Action>,
    pub allow_result: bool,
    pub allow_context: bool,
}

impl Checker<'_, '_> {
    pub fn require(
        &self,
        node: &ast::Node<Expression<'_>>,
        ty: &TypeRef,
    ) -> CheckedResult<Checked> {
        let result = self.check(node, Some(ty))?;
        if compatible(self.facts, &result.ty, ty) {
            Ok(result)
        } else {
            Err(self.mismatch(node.span, ty, &result.ty))
        }
    }

    pub fn check(
        &self,
        node: &ast::Node<Expression<'_>>,
        hint: Option<&TypeRef>,
    ) -> CheckedResult<Checked> {
        match &node.value {
            Expression::Literal(value) => self.literal(node.span, value, hint),
            Expression::Symbol(path) => {
                self.symbol(&path.segments, node.span, hint)
            },
            Expression::Grouped(inner) => self.check(inner, hint),
            Expression::Member { base, fields } => {
                if self.allow_context &&
                    let Expression::Symbol(path) = &base.value &&
                    matches!(path.segments.as_slice(), [name] if name.value == "context")
                {
                    return self.context(fields, node.span);
                }
                let base = self.check(base, None)?;
                self.members(base, fields)
            },
            Expression::Unary { operators, operand } => {
                if let [operator] = operators.as_slice() &&
                    operator.value == UnaryOp::Negate &&
                    let Some(text) = integer_literal(&operand.value)
                {
                    return self.negative_integer(node.span, text, hint);
                }
                let operand = self.check(operand, hint)?;
                let primitive = self.primitive(&operand.ty);
                for operator in operators {
                    let valid = match operator.value {
                        UnaryOp::Not => primitive == Some(Primitive::Bool),
                        UnaryOp::Negate => {
                            primitive.is_some_and(signed_or_float)
                        },
                    };
                    if !valid {
                        return Err(self.error(
                            operator.span,
                            "Invalid operand for prefix operator",
                        ));
                    }
                }
                Ok(Checked {
                    ty: operand.ty.clone(),
                    value: Value::Unary(
                        operators.iter().map(|op| op.value).collect(),
                        Box::new(operand),
                    ),
                })
            },
            Expression::Binary { first, rest } => {
                self.binary(first, rest, hint)
            },
        }
    }

    /// Resolves an effect from its action parameter, retaining every hop for
    /// two parameters that refer to the same entity type.
    pub fn effect_path(
        &self,
        path: &ast::Node<ast::Path<'_>>,
    ) -> CheckedResult<Checked> {
        let Some((parameter, fields)) = path.value.segments.split_first()
        else {
            return Err(self.scope.error(
                path.span,
                Code::InvalidActionContract,
                "An effect needs a parameter target",
            ));
        };
        let root = self.parameter(parameter).ok_or_else(|| {
            self.scope.error(
                parameter.span,
                Code::InvalidActionContract,
                "An effect must start at a declared action parameter",
            )
        })??;
        self.members(root, fields)
    }

    fn context(
        &self,
        fields: &[ast::Name<'_>],
        span: ast::Span,
    ) -> CheckedResult<Checked> {
        if let [field] = fields &&
            let Some(primitive) = self.environment.context.get(field.value)
        {
            return Ok(Checked {
                ty: (*primitive).into(),
                value: Value::Context(self.scope.name(field)?),
            });
        }
        Err(self.scope.error(
            span,
            Code::InvalidPropertyAccess,
            "Unknown contextual property",
        ))
    }

    fn parameter(
        &self,
        name: &ast::Name<'_>,
    ) -> Option<CheckedResult<Checked>> {
        let ty = self.action?.parameters.get(name.value)?;
        Some(self.scope.name(name).map(|name| Checked {
            ty: ty.clone(),
            value: Value::Parameter(name),
        }))
    }

    fn symbol(
        &self,
        path: &[ast::Name<'_>],
        span: ast::Span,
        hint: Option<&TypeRef>,
    ) -> CheckedResult<Checked> {
        if let [name] = path {
            if let Some(parameter) = self.parameter(name) {
                return parameter;
            }
            if name.value == "result" &&
                self.allow_result &&
                let Some(action) = self.action &&
                !compatible(
                    self.facts,
                    &action.result,
                    &Primitive::Unit.into(),
                )
            {
                return Ok(Checked {
                    ty: action.result.clone(),
                    value: Value::Result,
                });
            }
            if let Some(subject) = self.subject &&
                let Some(value) = self.scoped_member(subject, name)?
            {
                return Ok(value);
            }
            if let Some(TypeRef::Named(reference)) =
                hint.map(|ty| peel(self.facts, ty)) &&
                matches!(
                    self.facts.get(reference),
                    Some(Declaration::Enum(_))
                )
            {
                return self.variant(reference, name);
            }
            return Err(self.scope.error(
                name.span,
                Code::InvalidPropertyAccess,
                format!("Unknown value or property {}", name.value),
            ));
        }
        if let Some((name, prefix)) = path.split_last() &&
            let Some(Target::Symbol(reference)) = self.scope.find(prefix)
        {
            let original = TypeRef::Named(reference.clone());
            if let TypeRef::Named(exact) = peel(self.facts, &original) {
                return self.variant(exact, name);
            }
        }
        Err(self.scope.error(
            span,
            Code::UnresolvedSymbol,
            format!("Unknown enum value {}", spelling(path)),
        ))
    }

    fn variant(
        &self,
        reference: &SymbolRef,
        name: &ast::Name<'_>,
    ) -> CheckedResult<Checked> {
        if let Some(Declaration::Enum(definition)) = self.facts.get(reference) &&
            definition.variants.contains(name.value)
        {
            return Ok(Checked {
                ty: TypeRef::Named(reference.clone()),
                value: Value::Variant(
                    reference.clone(),
                    self.scope.name(name)?,
                ),
            });
        }
        Err(self.scope.error(
            name.span,
            Code::UnresolvedSymbol,
            format!("Unknown enum variant {}", name.value),
        ))
    }

    fn scoped_member(
        &self,
        subject: &SymbolRef,
        name: &ast::Name<'_>,
    ) -> CheckedResult<Option<Checked>> {
        let Some(declaration) = self.facts.get(subject) else {
            return Ok(None);
        };
        if let Declaration::View(view) = declaration {
            return view
                .outputs
                .get(name.value)
                .map(|output| {
                    Ok(Checked {
                        ty: output.result.clone(),
                        value: Value::Field {
                            base: None,
                            path: vec![FieldRef::new(
                                subject.clone(),
                                self.scope.name(name)?,
                            )],
                        },
                    })
                })
                .transpose();
        }
        let field = named_fields(declaration)
            .and_then(|fields| fields.get(name.value));
        let role = match declaration {
            Declaration::Relation(relation) => relation.roles.get(name.value),
            _ => None,
        };
        match (field, role) {
            (Some(_), Some(_)) => Err(self.scope.error(
                name.span,
                Code::InvalidPropertyAccess,
                "A field and role share this spelling",
            )),
            (Some(_), None) => {
                let (ty, field) = self.field(subject, name)?;
                Ok(Some(Checked {
                    ty,
                    value: Value::Field {
                        base: None,
                        path: vec![field],
                    },
                }))
            },
            (None, Some(role)) => {
                if (role.cardinality.min(), role.cardinality.max()) !=
                    (1, Some(1))
                {
                    return Err(self.scope.error(
                        name.span,
                        Code::UnsupportedSemanticConstruct,
                        "Expressions can traverse only singleton roles",
                    ));
                }
                Ok(Some(Checked {
                    ty: TypeRef::Entity(role.entity.clone()),
                    value: Value::Role {
                        relation: subject.clone(),
                        role: self.scope.name(name)?,
                    },
                }))
            },
            (None, None) => Ok(None),
        }
    }

    fn members(
        &self,
        mut base: Checked,
        fields: &[ast::Name<'_>],
    ) -> CheckedResult<Checked> {
        for name in fields {
            let owner = match peel(self.facts, &base.ty) {
                TypeRef::Entity(owner) | TypeRef::Named(owner) => owner.clone(),
                TypeRef::External(_) => return Err(self.scope.error(name.span, Code::UnsupportedSemanticConstruct, "External descriptors do not supply checked field types")),
                _ => return Err(self.scope.error(name.span, Code::InvalidPropertyAccess, "Property access requires an entity reference or structured value")),
            };
            let (ty, field) = self.field(&owner, name)?;
            // A long reference chain is legal. Flatten it so comparison and
            // dropping the checked tree do not recurse once per property.
            if let Value::Field { path, .. } = &mut base.value {
                path.push(field);
                base.ty = ty;
            } else {
                base = Checked {
                    ty,
                    value: Value::Field {
                        base: Some(Box::new(base)),
                        path: vec![field],
                    },
                };
            }
        }
        Ok(base)
    }

    fn field(
        &self,
        owner: &SymbolRef,
        name: &ast::Name<'_>,
    ) -> CheckedResult<(TypeRef, FieldRef)> {
        let field = self
            .facts
            .get(owner)
            .and_then(named_fields)
            .and_then(|fields| fields.get(name.value))
            .ok_or_else(|| {
                self.scope.error(
                    name.span,
                    Code::InvalidPropertyAccess,
                    format!("Unknown field {}", name.value),
                )
            })?;
        let ty = field_type(self.facts, field).ok_or_else(|| {
            self.scope.error(
                name.span,
                Code::UnsupportedSemanticConstruct,
                "This multiplicity has no bounded expression type",
            )
        })?;
        Ok((ty, FieldRef::new(owner.clone(), self.scope.name(name)?)))
    }

    /// Infer numeric literals from an explicit result or a typed operand. All
    /// other operands keep their declared type, including narrow field types.
    fn binary(
        &self,
        first: &ast::Node<Expression<'_>>,
        rest: &[ast::BinaryTerm<'_>],
        hint: Option<&TypeRef>,
    ) -> CheckedResult<Checked> {
        let hint = hint.filter(|ty| {
            rest.first()
                .is_some_and(|term| arithmetic(term.operator.value)) &&
                self.primitive(ty).is_some_and(numeric)
        });
        let inferred =
            if hint.is_none() && numeric_literal_expression(&first.value) {
                rest.iter()
                    .enumerate()
                    .find(|(_, term)| {
                        !numeric_literal_expression(&term.operand.value)
                    })
                    .map(|(position, term)| {
                        self.check(&term.operand, None)
                            .map(|checked| (position, checked))
                    })
                    .transpose()?
            } else {
                None
            };
        let first_hint =
            hint.or(inferred.as_ref().map(|(_, operand)| &operand.ty));
        let first = self.check(first, first_hint)?;
        let mut result_type = first.ty.clone();
        let mut terms = Vec::with_capacity(rest.len());
        let mut inferred = inferred;
        for (position, term) in rest.iter().enumerate() {
            let operand = match inferred.take() {
                Some((index, operand)) if index == position => operand,
                saved => {
                    inferred = saved;
                    self.check(&term.operand, Some(&result_type))?
                },
            };
            result_type = self.binary_type(
                term.operator.span,
                term.operator.value,
                &result_type,
                &operand.ty,
            )?;
            terms.push((term.operator.value, operand));
        }
        Ok(Checked {
            ty: result_type,
            value: Value::Binary(Box::new(first), terms),
        })
    }

    fn binary_type(
        &self,
        span: ast::Span,
        op: BinaryOp,
        left: &TypeRef,
        right: &TypeRef,
    ) -> CheckedResult<TypeRef> {
        if !compatible(self.facts, left, right) {
            return Err(self.mismatch(span, left, right));
        }
        let primitive = self.primitive(left);
        let valid = match op {
            BinaryOp::Or | BinaryOp::And => primitive == Some(Primitive::Bool),
            BinaryOp::Equal | BinaryOp::NotEqual => {
                self.comparable(left, TypeCapability::Equality)
            },
            BinaryOp::Less |
            BinaryOp::LessEqual |
            BinaryOp::Greater |
            BinaryOp::GreaterEqual => {
                self.comparable(left, TypeCapability::Ordering)
            },
            BinaryOp::Add |
            BinaryOp::Subtract |
            BinaryOp::Multiply |
            BinaryOp::Divide => primitive.is_some_and(numeric),
            BinaryOp::Remainder => primitive.and_then(integer_bits).is_some(),
        };
        if !valid {
            return Err(self.error(span, "Operator requires a compatible type with the appropriate capability"));
        }
        Ok(if arithmetic(op) {
            left.clone()
        } else {
            Primitive::Bool.into()
        })
    }

    fn comparable(&self, ty: &TypeRef, capability: TypeCapability) -> bool {
        let ordering = capability == TypeCapability::Ordering;
        match peel(self.facts, ty) {
            TypeRef::Primitive(primitive) => {
                if ordering {
                    integer_bits(*primitive).is_some() ||
                        matches!(
                            primitive,
                            Primitive::Char | Primitive::String
                        )
                } else {
                    *primitive != Primitive::Unit
                }
            },
            TypeRef::External(id) => self
                .environment
                .external_types
                .get(id)
                .is_some_and(|d| d.capabilities.contains(&capability)),
            TypeRef::Entity(_) => !ordering,
            TypeRef::Named(reference) => {
                !ordering &&
                    matches!(
                        self.facts.get(reference),
                        Some(Declaration::Enum(_))
                    )
            },
            TypeRef::Option(_) | TypeRef::List { .. } => false,
        }
    }

    fn literal(
        &self,
        span: ast::Span,
        literal: &ast::Literal<'_>,
        hint: Option<&TypeRef>,
    ) -> CheckedResult<Checked> {
        let (primitive, literal) = match literal {
            ast::Literal::Bool(value) => {
                (Primitive::Bool, Literal::Bool(*value))
            },
            ast::Literal::Char(value) => {
                (Primitive::Char, Literal::Char(*value))
            },
            ast::Literal::String(value) => {
                (Primitive::String, Literal::String(value.as_ref().into()))
            },
            ast::Literal::Integer(text) => {
                let primitive = hint
                    .and_then(|ty| self.primitive(ty))
                    .filter(|ty| integer_bits(*ty).is_some())
                    .unwrap_or(Primitive::I64);
                let value = text.parse::<u128>().ok().filter(|value| {
                    integer_bits(primitive).is_some_and(|bits| bits == 128 || (*value >> bits) == 0)
                }).ok_or_else(|| self.error(span, "Integer literal is out of range for the inferred type"))?;
                (primitive, Literal::Integer(value))
            },
            ast::Literal::Float(text) => {
                let primitive = hint
                    .and_then(|ty| self.primitive(ty))
                    .filter(|ty| floating(*ty))
                    .unwrap_or(Primitive::F64);
                let bits = if primitive == Primitive::F32 {
                    text.parse::<f32>().ok().filter(|v| v.is_finite()).map(|v| u64::from(v.to_bits()))
                } else {
                    text.parse::<f64>().ok().filter(|v| v.is_finite()).map(f64::to_bits)
                }.ok_or_else(|| self.error(span, "Floating-point literal must be finite and representable"))?;
                (primitive, Literal::Float(bits))
            },
        };
        Ok(Checked {
            ty: primitive.into(),
            value: Value::Literal(literal),
        })
    }

    fn primitive(&self, ty: &TypeRef) -> Option<Primitive> {
        match peel(self.facts, ty) {
            TypeRef::Primitive(primitive) => Some(*primitive),
            _ => None,
        }
    }

    fn negative_integer(
        &self,
        span: ast::Span,
        text: &str,
        hint: Option<&TypeRef>,
    ) -> CheckedResult<Checked> {
        let primitive = hint
            .and_then(|ty| self.primitive(ty))
            .filter(|primitive| integer_bits(*primitive).is_some())
            .unwrap_or(Primitive::I64);
        let bits = integer_bits(primitive)
            .filter(|_| signed_integer(primitive))
            .ok_or_else(|| {
                self.error(
                    span,
                    "A negative literal requires a signed integer type",
                )
            })?;
        let value = text.parse::<u128>().ok().filter(|value| *value <= (1_u128 << bits))
            .ok_or_else(|| self.error(span, "Negative integer literal is out of range for the inferred type"))?;
        let literal = if value == 0 {
            Literal::Integer(0)
        } else {
            Literal::NegativeInteger(value)
        };
        Ok(Checked {
            ty: primitive.into(),
            value: Value::Literal(literal),
        })
    }

    fn mismatch(
        &self,
        span: ast::Span,
        expected: &TypeRef,
        actual: &TypeRef,
    ) -> Diagnostic {
        self.error(span, format!("Expected {expected:?}, found {actual:?}"))
    }

    fn error(
        &self,
        span: ast::Span,
        message: impl Into<Box<str>>,
    ) -> Diagnostic {
        self.scope.error(span, Code::TypeMismatch, message)
    }
}

fn arithmetic(op: BinaryOp) -> bool {
    matches!(
        op,
        BinaryOp::Add |
            BinaryOp::Subtract |
            BinaryOp::Multiply |
            BinaryOp::Divide |
            BinaryOp::Remainder
    )
}

fn integer_literal<'a>(mut expression: &'a Expression<'a>) -> Option<&'a str> {
    loop {
        match expression {
            Expression::Grouped(inner) => expression = &inner.value,
            Expression::Literal(ast::Literal::Integer(text)) => {
                return Some(text);
            },
            _ => return None,
        }
    }
}

fn numeric_literal_expression(expression: &Expression<'_>) -> bool {
    match expression {
        Expression::Literal(
            ast::Literal::Integer(_) | ast::Literal::Float(_),
        ) => true,
        Expression::Grouped(inner) => numeric_literal_expression(&inner.value),
        Expression::Unary { operand, operators } => {
            operators
                .iter()
                .all(|operator| operator.value == UnaryOp::Negate) &&
                numeric_literal_expression(&operand.value)
        },
        Expression::Binary { first, rest } => {
            numeric_literal_expression(&first.value) &&
                rest.iter().all(|term| {
                    arithmetic(term.operator.value) &&
                        numeric_literal_expression(&term.operand.value)
                })
        },
        _ => false,
    }
}

fn integer_bits(primitive: Primitive) -> Option<u32> {
    Some(match primitive {
        Primitive::I8 => 7,
        Primitive::I16 => 15,
        Primitive::I32 => 31,
        Primitive::I64 => 63,
        Primitive::I128 => 127,
        Primitive::U8 => 8,
        Primitive::U16 => 16,
        Primitive::U32 => 32,
        Primitive::U64 => 64,
        Primitive::U128 => 128,
        _ => return None,
    })
}

fn floating(primitive: Primitive) -> bool {
    matches!(primitive, Primitive::F32 | Primitive::F64)
}

fn numeric(primitive: Primitive) -> bool {
    integer_bits(primitive).is_some() || floating(primitive)
}

fn signed_integer(primitive: Primitive) -> bool {
    matches!(
        primitive,
        Primitive::I8 |
            Primitive::I16 |
            Primitive::I32 |
            Primitive::I64 |
            Primitive::I128
    )
}

fn signed_or_float(primitive: Primitive) -> bool {
    signed_integer(primitive) || floating(primitive)
}

pub(crate) fn dependencies(expression: &Checked) -> BTreeSet<Reference> {
    let mut dependencies = BTreeSet::new();
    let mut pending = vec![expression];
    while let Some(value) = pending.pop() {
        match &value.value {
            Value::Field { base, path } => {
                dependencies
                    .extend(path.iter().cloned().map(Reference::Field));
                pending.extend(base.iter().map(Box::as_ref));
            },
            Value::Role { relation, .. } | Value::Variant(relation, _) => {
                dependencies.insert(relation.clone().into());
            },
            Value::Unary(_, operand) => pending.push(operand),
            Value::Binary(first, terms) => {
                pending.push(first);
                pending.extend(terms.iter().map(|(_, operand)| operand));
            },
            _ => {},
        }
    }
    dependencies
}
