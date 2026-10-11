//! Private authentication persistence shared by HTTP adapters and integration tests.

mod api;
mod store;

pub use api::router;
