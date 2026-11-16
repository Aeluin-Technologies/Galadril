//! Resolve GOL projects and check their declarations against local contracts.

mod analysis;
mod contracts;
mod diagnostic;
mod expression;
mod index;
mod ir;
mod types;

pub use analysis::analyze_project;
pub use diagnostic::{Code, Diagnostic, Diagnostics, Location};
#[cfg(feature = "authorization")]
pub(crate) use expression::dependencies as expression_dependencies;
pub use ir::{
    Binding, Checked, Dependency, DependencyKind, DependencyTarget,
    EffectPath, Environment, LabelTransfer, Literal, PermissionRef, Policy,
    Project, Security, ValidationState, Value,
};
