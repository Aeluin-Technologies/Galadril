//! Galadril Scribe.

pub mod engine;
pub mod tools;

#[cfg(feature = "latex")]
pub use engine::report::ScribeReport;
pub use engine::{ScribeChat, ScribeConfig};
#[cfg(feature = "latex")]
pub use tools::add_section::Section;
pub use tools::database::{DatabaseProvider, NoOpProvider};
