//! Durable orchestration and isolated native PDF compilation.
pub mod adapters;
pub mod application;
pub mod grpc;
pub mod ports;
pub mod sandbox_policy;
#[cfg(all(target_os = "linux", feature = "supervisor"))]
pub mod supervisor;
#[cfg(all(target_os = "linux", feature = "worker"))]
pub mod worker;

pub mod domain;

#[cfg(all(target_os = "linux", feature = "worker"))]
pub mod kernel;
