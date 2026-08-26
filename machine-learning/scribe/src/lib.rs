//! Galadril Scribe.

pub mod config;
pub mod engine;
#[cfg(feature = "latex")]
pub mod report;
mod session;
mod stream;
pub mod telemetry;
pub mod tools;

pub use engine::{
    ScribeCompletionEvent, ScribeCompletionStatus, ScribeConfig, ScribeEngine,
    ScribeModelConfig, ScribeModelPreset, ScribeRequest,
};
#[cfg(feature = "latex")]
pub use report::ScribeReport;
pub use telemetry::ScribeMetricsSnapshot;
#[cfg(feature = "latex")]
pub use tools::add_section::Section;
pub use tools::database::{DatabaseProvider, NoOpProvider};
