//! Bounded invariant checks over facts already admitted by the trusted host.
//!
//! A successful check satisfies these predicates only. It grants no access,
//! validates no reference existence, and authorizes no mutation. Facts and
//! control dependencies must retain their security obligations at the host.

#[cfg(all(feature = "archive", feature = "parser"))]
mod compile;
mod evaluate;
mod scalar;

use std::fmt;

#[cfg(all(feature = "archive", feature = "parser"))]
pub(crate) use compile::contracts;
pub use scalar::Scalar;

use crate::checked::{Checked, Value};
use crate::{FieldRef, Name, Primitive, QualifiedName, SymbolRef, TypeRef};

/// Resource ceilings for untrusted encodings and bounded pure evaluation.
///
/// Callers may lower these values. Raising a ceiling beyond its default is
/// rejected so a request cannot disable the reader's safety boundary.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// Maximum encoded contract bytes.
    pub max_bytes: usize,
    /// Maximum nested JSON containers, AST nodes, or type constructors.
    pub max_depth: usize,
    /// Maximum AST nodes, operators, path members, and type constructors.
    pub max_nodes: usize,
    /// Maximum evaluation steps, including bounded text comparisons.
    pub max_steps: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_bytes: 64 * 1024,
            max_depth: 64,
            max_nodes: 4096,
            max_steps: 8192,
        }
    }
}

impl Limits {
    fn bounded(self) -> Result<Self, Error> {
        let ceiling = Self::default();
        if self.max_bytes > ceiling.max_bytes ||
            self.max_depth > ceiling.max_depth ||
            self.max_nodes > ceiling.max_nodes ||
            self.max_steps > ceiling.max_steps
        {
            Err(Error::Budget)
        } else {
            Ok(self)
        }
    }
}

/// A metadata-safe reason an invariant contract cannot accept a proposal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Error {
    /// An encoding, nesting, node, text, or execution ceiling was reached.
    Budget,
    /// The contract encoding or expression shape is invalid.
    Invalid,
    /// An admitted value required by the expression was not supplied.
    MissingBinding,
    /// A reference, external capability, or operation needs host support.
    Unsupported,
    /// A supplied scalar does not have the exact checked GOL type.
    TypeMismatch,
    /// Arithmetic overflowed, divided by zero, or produced a nonfinite value.
    Arithmetic,
    /// A well-formed predicate evaluated to false.
    Violation,
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Budget => "GOL invariant budget exceeded",
            Self::Invalid => "invalid GOL invariant contract",
            Self::MissingBinding => "GOL invariant fact unavailable",
            Self::Unsupported => "GOL invariant capability unavailable",
            Self::TypeMismatch => "GOL invariant fact type mismatch",
            Self::Arithmetic => "GOL invariant arithmetic rejected",
            Self::Violation => "GOL invariant rejected the proposal",
        })
    }
}
impl std::error::Error for Error {}

/// Authorized, typed facts for one proposal and its trusted context.
///
/// Implementations must bind the complete path and explicit base to the
/// correct instance. They must not perform implicit reference traversal or
/// replace a missing fact with a default. This interface makes no I/O calls.
pub trait Bindings {
    /// Returns a pre-admitted field, preserving intermediate owners and base.
    fn field<'a>(
        &'a self,
        _base: Option<&Checked>,
        _path: &[FieldRef],
        _expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        Err(Error::MissingBinding)
    }

    /// Returns a typed context fact verified by the host, not request JSON.
    fn context<'a>(
        &'a self,
        _name: &Name,
        _expected: &TypeRef,
    ) -> Result<Scalar<'a>, Error> {
        Err(Error::MissingBinding)
    }
}

/// One checked, named state requirement retained in an owner contract.
#[cfg_attr(feature = "archive", derive(serde::Serialize, serde::Deserialize))]
#[cfg_attr(feature = "archive", serde(deny_unknown_fields))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Predicate {
    name: QualifiedName,
    body: Checked,
}

impl Predicate {
    /// Borrows the compiler identity for trusted host dependency resolution.
    #[must_use]
    pub fn name(&self) -> &QualifiedName {
        &self.name
    }

    /// Borrows the normalized checked body, not source syntax.
    #[must_use]
    pub fn body(&self) -> &Checked {
        &self.body
    }
}

/// Invariants pinned to one logical owner and ontology version.
///
/// Contract metadata may itself be confidential. Obtain it from an
/// authenticated archive through `invariant_contract`; do not expose it in
/// API diagnostics or use an untrusted JSON contract to authorize effects.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Contract {
    owner: SymbolRef,
    predicates: Vec<Predicate>,
}

impl Contract {
    /// Returns the exact owner, including its declared ontology version.
    #[must_use]
    pub fn owner(&self) -> &SymbolRef {
        &self.owner
    }

    /// Borrows predicates in deterministic definition-name order.
    #[must_use]
    pub fn predicates(&self) -> &[Predicate] {
        &self.predicates
    }

    /// Decodes an authenticated contract within the supplied hard ceilings.
    ///
    /// This checks structure and budgets, not artifact provenance or the
    /// semantic authority of names. The archive reader verifies those pins.
    #[cfg(feature = "archive")]
    pub fn decode(bytes: &[u8], limits: Limits) -> Result<Self, Error> {
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            owner: SymbolRef,
            predicates: Vec<Predicate>,
        }
        let limits = limits.bounded()?;
        preflight(bytes, limits)?;
        let wire: Wire =
            serde_json::from_slice(bytes).map_err(|_| Error::Invalid)?;
        let contract = Self {
            owner: wire.owner,
            predicates: wire.predicates,
        };
        contract.structure(limits)?;
        Ok(contract)
    }

    /// Checks every predicate without allocating intermediate values.
    ///
    /// Both Boolean operands are checked. A true disjunction cannot hide a
    /// missing or unauthorized dependency. The caller must still enforce
    /// SpiceDB, Cedar, IFC, reference validity, and atomic write semantics.
    ///
    /// ```
    /// use galadril_ontology::validation::{Bindings, Contract, Error, Limits};
    ///
    /// struct AdmittedFacts;
    /// impl Bindings for AdmittedFacts {}
    ///
    /// // An authorized archive reader supplies the owner-pinned contract.
    /// fn check_proposal(contract: &Contract) -> Result<(), Error> {
    ///     contract.check(&AdmittedFacts, Limits::default())
    /// }
    /// ```
    pub fn check(
        &self,
        bindings: &impl Bindings,
        limits: Limits,
    ) -> Result<(), Error> {
        let span = tracing::debug_span!("gol.invariant.check");
        let _guard = span.enter();
        let limits = limits.bounded()?;
        self.structure(limits)?;
        let mut budget = Budget::new(limits);
        for predicate in &self.predicates {
            match evaluate::evaluate(
                &predicate.body,
                bindings,
                &mut budget,
                0,
            )? {
                Scalar::Bool(true) => {},
                Scalar::Bool(false) => return Err(Error::Violation),
                _ => return Err(Error::TypeMismatch),
            }
        }
        Ok(())
    }

    fn structure(&self, limits: Limits) -> Result<(), Error> {
        let mut budget = Budget::new(limits);
        let mut previous = None;
        for predicate in &self.predicates {
            if previous.is_some_and(|name| name >= &predicate.name) {
                return Err(Error::Invalid);
            }
            previous = Some(&predicate.name);
            if predicate.body.ty != TypeRef::Primitive(Primitive::Bool) {
                return Err(Error::TypeMismatch);
            }
            visit(&predicate.body, &mut budget, 0)?;
        }
        Ok(())
    }
}

struct Budget {
    limits: Limits,
    nodes: usize,
    steps: usize,
}
impl Budget {
    fn new(limits: Limits) -> Self {
        Self {
            limits,
            nodes: 0,
            steps: 0,
        }
    }

    fn node(&mut self, depth: usize, count: usize) -> Result<(), Error> {
        self.nodes = self.nodes.checked_add(count).ok_or(Error::Budget)?;
        if depth > self.limits.max_depth || self.nodes > self.limits.max_nodes
        {
            Err(Error::Budget)
        } else {
            Ok(())
        }
    }

    fn step(&mut self, count: usize) -> Result<(), Error> {
        self.steps = self.steps.checked_add(count).ok_or(Error::Budget)?;
        if self.steps > self.limits.max_steps {
            Err(Error::Budget)
        } else {
            Ok(())
        }
    }
}

fn visit(
    value: &Checked,
    budget: &mut Budget,
    depth: usize,
) -> Result<(), Error> {
    budget.node(depth, 1)?;
    visit_type(&value.ty, budget, 0)?;
    match &value.value {
        Value::Field { base, path } => {
            if path.is_empty() {
                return Err(Error::Invalid);
            }
            budget.node(depth, path.len())?;
            if let Some(base) = base {
                visit(base, budget, depth + 1)?;
            }
        },
        Value::Unary(operators, operand) => {
            if operators.is_empty() {
                return Err(Error::Invalid);
            }
            budget.node(depth, operators.len())?;
            visit(operand, budget, depth + 1)?;
        },
        Value::Binary(first, rest) => {
            if rest.is_empty() {
                return Err(Error::Invalid);
            }
            budget.node(depth, rest.len())?;
            visit(first, budget, depth + 1)?;
            for (_, operand) in rest {
                visit(operand, budget, depth + 1)?;
            }
        },
        _ => {},
    }
    Ok(())
}

fn visit_type(
    value: &TypeRef,
    budget: &mut Budget,
    depth: usize,
) -> Result<(), Error> {
    budget.node(depth, 1)?;
    match value {
        TypeRef::Option(inner) | TypeRef::List { element: inner, .. } => {
            visit_type(inner, budget, depth + 1)
        },
        _ => Ok(()),
    }
}

#[cfg(feature = "archive")]
fn preflight(bytes: &[u8], limits: Limits) -> Result<(), Error> {
    if bytes.len() > limits.max_bytes {
        return Err(Error::Budget);
    }
    let mut depth = 0_usize;
    let mut string = false;
    let mut escaped = false;
    for byte in bytes {
        if string {
            if escaped {
                escaped = false;
            } else if *byte == b'\\' {
                escaped = true;
            } else if *byte == b'"' {
                string = false;
            }
        } else {
            match byte {
                b'"' => string = true,
                b'{' | b'[' => {
                    depth = depth.checked_add(1).ok_or(Error::Budget)?;
                    if depth > limits.max_depth {
                        return Err(Error::Budget);
                    }
                },
                b'}' | b']' => {
                    depth = depth.checked_sub(1).ok_or(Error::Invalid)?
                },
                _ => {},
            }
        }
    }
    if depth != 0 || string {
        Err(Error::Invalid)
    } else {
        Ok(())
    }
}
