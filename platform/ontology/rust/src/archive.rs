//! Authenticated local GOLC archives and permission-checked sparse lookup.

mod container;
mod reader;
#[cfg(feature = "remote")]
pub mod remote;
#[cfg(feature = "parser")]
mod writer;

use std::{fmt, io};

pub use reader::{ArchiveReader, LocalFile, RangeReader, RangeRequest};
use serde::Deserialize;
#[cfg(feature = "parser")]
use serde::Serialize;
#[cfg(feature = "parser")]
pub use writer::{CompiledArchive, compile_archive};

use crate::{Primitive, Reference, SymbolKind};

/// A SHA-256 pin for the immutable GOLC manifest header.
///
/// Obtain it through the trusted publication channel. Pins derived from
/// untrusted archive bytes provide no provenance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ArchivePin(pub [u8; 32]);

/// Reasons a trusted host cannot open or publish an archive.
///
/// Query methods intentionally use a single unavailable result.
#[derive(Debug)]
#[non_exhaustive]
pub enum ArchiveError {
    /// A byte range could not be read.
    Io(io::Error),
    /// The header or block format revision is unsupported.
    Version,
    /// Integrity, bounds, structure, or index validation failed.
    Invalid,
    /// The compiler could not encode a checked project within format bounds.
    Encoding,
    /// Mandatory security compilation did not produce a publishable contract.
    #[cfg(feature = "parser")]
    Security(Vec<crate::authorization::Issue>),
}

impl fmt::Display for ArchiveError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Io(_) => "GOLC I/O failure",
            Self::Version => "unsupported GOLC version",
            Self::Invalid => "invalid GOLC archive",
            Self::Encoding => "GOLC encoding failed",
            #[cfg(feature = "parser")]
            Self::Security(_) => "GOLC security compilation failed",
        })
    }
}

impl std::error::Error for ArchiveError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for ArchiveError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Private failure of an invariant lookup by a trusted runtime.
#[derive(Debug)]
pub enum InvariantContractError<E> {
    /// The security decision withheld the contract.
    Denied(crate::authorization::Denial),
    /// The reader could not verify or load the contract.
    Archive(E),
}

impl From<ArchiveError> for InvariantContractError<ArchiveError> {
    fn from(error: ArchiveError) -> Self {
        Self::Archive(error)
    }
}

/// A disclosure of the exact symbol or member authorized by the reader.
///
/// Properties are separate records so checking a symbol never exposes its
/// field names, policy bindings, or raw definition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Symbol {
    /// The exact identity, including the pinned ontology version.
    pub target: Reference,
    /// Category of the owning declaration.
    pub kind: SymbolKind,
    /// Type of a scalar field when its representation names no other symbol.
    pub primitive: Option<Primitive>,
}

/// The orientation of a business or declaration dependency lookup.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    /// Select a target referenced by the anchor.
    Direct,
    /// Select a source that references the anchor.
    Inverse,
    /// Inspect any direct local dependency on the other endpoint.
    Dependency,
}

#[cfg(feature = "parser")]
pub(crate) fn encode(value: &impl Serialize) -> Result<Vec<u8>, ArchiveError> {
    serde_json::to_vec(value).map_err(|_| ArchiveError::Encoding)
}

pub(crate) fn decode<T: for<'de> Deserialize<'de>>(
    bytes: &[u8],
) -> Result<T, ArchiveError> {
    serde_json::from_slice(bytes).map_err(|_| ArchiveError::Invalid)
}
