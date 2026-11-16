//! Versioned GOL security contracts and a single conjunctive decision gate.
//!
//! Compilation takes approved authority metadata. The gate takes request
//! evidence from a trusted runtime and queries current relationship
//! permissions.

#[cfg(feature = "archive")]
pub(crate) mod archive;

mod computation;
mod contract;
mod decision;
mod execution;
mod flow;
mod kernel;

#[cfg(feature = "parser")]
mod cedar;
#[cfg(feature = "parser")]
mod compiler;
#[cfg(feature = "parser")]
mod environment;
#[cfg(feature = "parser")]
mod propagation;

#[cfg(feature = "parser")]
pub use compiler::{Compilation, Issue, IssueKind, compile};
pub use computation::{Clearance, Computation, FlowError};
pub use contract::{
    AuthorityRevision, Contract, Descriptor, Operation, Route, Versions,
};
pub use decision::{
    Access, Consistency, Decision, Denial, EffectError, EffectReceipt,
    Evidence, Instance, InstanceEvidence, PermissionCheck, PermissionOutcome,
    PermissionProvider, Principal, TrustedRuntime, authorize, perform,
};
#[cfg(feature = "parser")]
pub use environment::{
    CedarInput, CedarResource, RelationshipInput, RelationshipResource,
    SecurityEnvironment,
};
pub use execution::ExecutionContext;
pub use flow::{Flow, Sink, SinkKind};
