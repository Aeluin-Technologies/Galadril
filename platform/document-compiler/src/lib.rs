//! Credential-free document compilation contracts shared by Gateway and
//! workers.
pub mod domain;

#[rustfmt::skip]
pub mod proto { include!("generated/galadril.document_compiler.v1.rs"); }

pub mod archive;
pub mod delegation;
pub mod tools;
