//! Registry client, storage, and canonical semantic validation.

#![deny(missing_docs)]

pub mod domain;
pub mod grpc;
pub mod semantics;
pub mod state;
pub mod storage;

#[rustfmt::skip]
#[allow(missing_docs, clippy::all)]
pub mod proto {
    include!("generated/galadril.registry.v1.rs");
}
