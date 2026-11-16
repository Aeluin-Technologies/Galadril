//! Diagnostics keep bounded telemetry codes separate from source presentation.

use std::error::Error;
use std::fmt::{self, Display, Formatter};

use miette::{Diagnostic, LabeledSpan, NamedSource, Report, SourceCode};

use super::ast::Span;

/// Stable categories of parser failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum ErrorCode {
    /// The input does not match the GOL grammar.
    Syntax,
    /// A syntactically spelled escape does not identify a Unicode scalar.
    InvalidEscape,
    /// The source exceeds the parser's byte budget.
    SourceTooLarge,
    /// The source exceeds the parser's delimiter nesting budget.
    NestingLimit,
    /// The source exceeds the parser's lexical work budget.
    TokenLimit,
    /// The grammar and its Rust AST conversion disagree.
    Internal,
}

impl ErrorCode {
    /// Returns the stable identifier used by miette and telemetry.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Syntax => "gol::syntax",
            Self::InvalidEscape => "gol::invalid_escape",
            Self::SourceTooLarge => "gol::source_too_large",
            Self::NestingLimit => "gol::nesting_limit",
            Self::TokenLimit => "gol::token_limit",
            Self::Internal => "gol::internal",
        }
    }

    fn message(self) -> &'static str {
        match self {
            Self::Syntax => "Invalid GOL syntax",
            Self::InvalidEscape => "Escape does not encode a Unicode scalar",
            Self::SourceTooLarge => "GOL source exceeds the byte limit",
            Self::NestingLimit => "GOL source exceeds the nesting limit",
            Self::TokenLimit => "GOL source exceeds the lexical unit limit",
            Self::Internal => "GOL parser produced an unexpected syntax node",
        }
    }
}

#[derive(Debug)]
pub(super) struct Failure {
    code: ErrorCode,
    span: Span,
    expected: Option<&'static str>,
}

impl Failure {
    pub(super) fn new(code: ErrorCode, span: Span) -> Self {
        Self {
            code,
            span,
            expected: None,
        }
    }

    pub(super) fn syntax(span: Span, expected: Option<&'static str>) -> Self {
        Self {
            code: ErrorCode::Syntax,
            span,
            expected,
        }
    }
}

/// A source-located parse failure. Source text can be attached for rendering.
#[derive(Debug)]
pub struct ParseError {
    source_name: Box<str>,
    failure: Failure,
}

impl ParseError {
    pub(super) fn new(source_name: &str, failure: Failure) -> Self {
        tracing::debug!(
            event.name = "gol.syntax.rejected",
            code = failure.code.as_str()
        );
        Self {
            source_name: source_name.into(),
            failure,
        }
    }

    /// Returns a stable code for programmatic handling.
    #[must_use]
    pub fn code(&self) -> ErrorCode {
        self.failure.code
    }

    /// Returns the caller-supplied source identity.
    #[must_use]
    pub fn source_name(&self) -> &str {
        &self.source_name
    }

    /// Returns the byte range of the failure, empty at end of source.
    #[must_use]
    pub fn span(&self) -> Span {
        self.failure.span
    }

    /// Returns an expected construct when the parser can identify one.
    #[must_use]
    pub fn expected(&self) -> Option<&'static str> {
        self.failure.expected
    }

    /// Attaches the original contents to a renderable miette diagnostic.
    ///
    /// Pass owned text or shared source storage such as an Arc of a string.
    #[must_use]
    pub fn with_source(self, text: impl SourceCode + 'static) -> Report {
        let source = NamedSource::new(self.source_name(), text);
        Report::new(self).with_source_code(source)
    }
}

impl Display for ParseError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} in {}",
            self.code().message(),
            self.source_name()
        )
    }
}

impl Error for ParseError {}

impl Diagnostic for ParseError {
    fn code(&self) -> Option<Box<dyn Display + '_>> {
        Some(Box::new(self.code().as_str()))
    }

    fn labels(&self) -> Option<Box<dyn Iterator<Item = LabeledSpan> + '_>> {
        let Span { start, end } = self.span();
        let label = self
            .expected()
            .map(|expected| format!("expected {expected}"));
        Some(Box::new(std::iter::once(
            LabeledSpan::new_primary_with_span(label, (start, end - start)),
        )))
    }
}
