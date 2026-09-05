//! Database adapter.

pub mod audit;
pub mod bootstrap;
pub mod connection;
pub mod control_plane;
pub mod conversations;
pub mod entity_states;
pub mod iam;
#[cfg(test)]
pub mod pipelines;
pub mod relations_age;
pub mod search;
pub mod user_directory;
