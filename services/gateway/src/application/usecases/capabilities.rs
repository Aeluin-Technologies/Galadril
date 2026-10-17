//! Discovery failures preserve the identity boundary without exposing
//! dependencies.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiscoveryError {
    Forbidden,
    Unavailable,
}

impl std::fmt::Display for DiscoveryError {
    fn fmt(
        &self,
        formatter: &mut std::fmt::Formatter<'_>,
    ) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Forbidden => "Capability catalog access denied",
            Self::Unavailable => "Capability catalog unavailable",
        })
    }
}

impl std::error::Error for DiscoveryError {}
