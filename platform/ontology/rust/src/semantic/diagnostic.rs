//! Stable diagnostic categories and source positions for semantic checks.

use std::error::Error;
use std::fmt::{self, Display, Formatter};

use miette::LabeledSpan;

use crate::syntax::Span;

/// Stable categories of project analysis errors.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum Code {
    /// A name is incompatible with the canonical model.
    InvalidName,
    /// A module version is invalid or incompatible with its selected version.
    VersionConflict,
    /// A declaration already uses this symbol in the same scope.
    DuplicateSymbol,
    /// A required namespace or selected export is missing.
    UnresolvedImport,
    /// More than one symbol or descriptor matches an import.
    AmbiguousImport,
    /// A requested symbol is absent from the visible scope.
    UnresolvedSymbol,
    /// A name is missing or does not denote a value type.
    UnknownType,
    /// The type of an expression or operand conflicts with its contract.
    TypeMismatch,
    /// A generic argument is incompatible with its constructor.
    InvalidGenericArgument,
    /// A role's participant or declaration is invalid.
    InvalidRelationRole,
    /// A cardinality is invalid or exceeds the supported representation.
    InvalidCardinality,
    /// A property or participant cannot be selected through this expression.
    InvalidPropertyAccess,
    /// An action's parameters, effects or retry declarations conflict.
    InvalidActionContract,
    /// A security declaration or security target is invalid.
    InvalidSecurityTarget,
    /// A structure, alias or derived definition is recursively dependent.
    Cycle,
    /// The syntax is recognized but its required semantics are undefined.
    UnsupportedSemanticConstruct,
}

impl Code {
    /// Returns the stable, source-independent diagnostic identifier.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InvalidName => "gol::semantic::invalid_name",
            Self::VersionConflict => "gol::semantic::version_conflict",
            Self::DuplicateSymbol => "gol::semantic::duplicate_symbol",
            Self::UnresolvedImport => "gol::semantic::unresolved_import",
            Self::AmbiguousImport => "gol::semantic::ambiguous_import",
            Self::UnresolvedSymbol => "gol::semantic::unresolved_symbol",
            Self::UnknownType => "gol::semantic::unknown_type",
            Self::TypeMismatch => "gol::semantic::type_mismatch",
            Self::InvalidGenericArgument => {
                "gol::semantic::invalid_generic_argument"
            },
            Self::InvalidRelationRole => {
                "gol::semantic::invalid_relation_role"
            },
            Self::InvalidCardinality => "gol::semantic::invalid_cardinality",
            Self::InvalidPropertyAccess => {
                "gol::semantic::invalid_property_access"
            },
            Self::InvalidActionContract => {
                "gol::semantic::invalid_action_contract"
            },
            Self::InvalidSecurityTarget => {
                "gol::semantic::invalid_security_target"
            },
            Self::Cycle => "gol::semantic::cycle",
            Self::UnsupportedSemanticConstruct => {
                "gol::semantic::unsupported_construct"
            },
        }
    }
}

/// A byte range in one caller-supplied source.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Location {
    /// Caller-supplied filename or source identifier.
    pub source: Box<str>,
    /// Inclusive start and exclusive end in source bytes.
    pub range: (usize, usize),
}

impl Location {
    pub(super) fn new(source: &str, span: Span) -> Self {
        Self {
            source: source.into(),
            range: (span.start, span.end),
        }
    }
}

/// A semantic error with a primary location and an optional conflict location.
#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Diagnostic {
    primary: Location,
    code: Code,
    message: Box<str>,
    related: Option<Location>,
}

impl Diagnostic {
    pub(super) fn new(
        source: &str,
        span: Span,
        code: Code,
        message: impl Into<Box<str>>,
    ) -> Self {
        Self {
            primary: Location::new(source, span),
            code,
            message: message.into(),
            related: None,
        }
    }

    pub(super) fn with_related(mut self, source: &str, span: Span) -> Self {
        self.related = Some(Location::new(source, span));
        self
    }

    /// Returns the stable error category.
    #[must_use]
    pub fn code(&self) -> Code {
        self.code
    }

    /// Returns the main location of the problem.
    #[must_use]
    pub fn location(&self) -> &Location {
        &self.primary
    }

    /// Returns the other declaration involved in a conflict.
    #[must_use]
    pub fn related(&self) -> Option<&Location> {
        self.related.as_ref()
    }
}

impl Display for Diagnostic {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        write!(f, "{} in {}", self.message, self.primary.source)
    }
}

impl Error for Diagnostic {}

impl miette::Diagnostic for Diagnostic {
    fn code(&self) -> Option<Box<dyn Display + '_>> {
        Some(Box::new(self.code.as_str()))
    }

    fn labels(&self) -> Option<Box<dyn Iterator<Item = LabeledSpan> + '_>> {
        let (start, end) = self.primary.range;
        let primary = LabeledSpan::new_primary_with_span(
            None,
            (start, end.saturating_sub(start)),
        );
        let related = self
            .related
            .as_ref()
            .filter(|r| r.source == self.primary.source)
            .map(|r| {
                LabeledSpan::new_with_span(
                    Some("related declaration".into()),
                    (r.range.0, r.range.1.saturating_sub(r.range.0)),
                )
            });
        Some(Box::new(std::iter::once(primary).chain(related)))
    }
}

/// The sorted, nonempty diagnostics of an unsuccessful compilation.
#[derive(Debug)]
pub struct Diagnostics(Vec<Diagnostic>);

impl Diagnostics {
    pub(super) fn new(mut diagnostics: Vec<Diagnostic>) -> Self {
        diagnostics.sort_unstable();
        diagnostics.dedup();
        for diagnostic in &diagnostics {
            tracing::debug!(
                event.name = "gol.semantic.rejected",
                code = diagnostic.code.as_str()
            );
        }
        Self(diagnostics)
    }

    /// Iterates in source, range and error-code order.
    pub fn iter(&self) -> impl ExactSizeIterator<Item = &Diagnostic> {
        self.0.iter()
    }
}

impl Display for Diagnostics {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        if let Some(first) = self.0.first() {
            write!(f, "{} GOL semantic error(s). {}", self.0.len(), first)
        } else {
            f.write_str("GOL semantic analysis failed")
        }
    }
}

impl Error for Diagnostics {}

pub(super) type CheckedResult<T> = Result<T, Diagnostic>;
