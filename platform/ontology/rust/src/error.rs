//! Construction errors with a bounded set of telemetry reason codes.

use std::error::Error;
use std::fmt::{self, Display, Formatter};

use crate::external_type::ExternalTypeId;
use crate::identity::{Name, OntologyId};

/// A rejected local model construction operation.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ModelError {
    /// The input is outside the canonical simple-name alphabet.
    InvalidName,
    /// At least one segment fails simple-name validation.
    InvalidQualifiedName,
    /// The scope already contains a declaration with the given name.
    DuplicateName {
        /// The conflicting name.
        name: Name,
    },
    /// The module already has an import for this ontology.
    DuplicateImport {
        /// The conflicting import target.
        ontology: OntologyId,
    },
    /// The registry already contains this exact external type version.
    DuplicateExternalType {
        /// The conflicting descriptor identity.
        external_type: ExternalTypeId,
    },
    /// The inclusive upper bound is less than the lower bound.
    InvalidCardinality {
        /// The requested lower bound.
        min: u32,
        /// The requested upper bound.
        max: u32,
    },
}

impl ModelError {
    pub(crate) fn record(self) -> Self {
        let reason = match &self {
            Self::InvalidName => "invalid_name",
            Self::InvalidQualifiedName => "invalid_qualified_name",
            Self::DuplicateName { .. } => "duplicate_name",
            Self::DuplicateImport { .. } => "duplicate_import",
            Self::DuplicateExternalType { .. } => "duplicate_external_type",
            Self::InvalidCardinality { .. } => "invalid_cardinality",
        };
        tracing::debug!(event.name = "gol.model.rejected", reason);
        self
    }
}

impl Display for ModelError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidName => formatter.write_str("invalid GOL name"),
            Self::InvalidQualifiedName => {
                formatter.write_str("invalid GOL qualified name")
            },
            Self::DuplicateName { name } => {
                write!(formatter, "duplicate GOL name `{name}`")
            },
            Self::DuplicateImport { ontology } => {
                write!(formatter, "duplicate GOL import `{ontology}`")
            },
            Self::DuplicateExternalType { external_type } => {
                write!(
                    formatter,
                    "duplicate GOL external type `{external_type}`"
                )
            },
            Self::InvalidCardinality { min, max } => write!(
                formatter,
                "invalid GOL cardinality: lower bound {min} exceeds {max}"
            ),
        }
    }
}

impl Error for ModelError {}
