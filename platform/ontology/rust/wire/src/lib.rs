//! Generated bindings for the GOLC block schema.
//!
//! Archive consumers always use the verifier before accessing a block.
//!
//! Unverified root helpers are deliberately outside this crate's public API.
//!
//! ```compile_fail
//! use golc_wire::root_as_block_unchecked;
//! ```

#![deny(unsafe_code)]

#[rustfmt::skip]
#[expect(
    unsafe_code,
    dead_code,
    clippy::extra_unused_lifetimes,
    clippy::missing_safety_doc,
    reason = "flatc 25.12.19 emits these bindings verbatim"
)]
mod block_generated;

pub use block_generated::{
    block_buffer_has_identifier, finish_block_buffer, root_as_block, Block,
    BlockArgs, DeclarationTag, Entry, EntryArgs, Kind, ScalarTag,
};
pub use flatbuffers::FlatBufferBuilder;
