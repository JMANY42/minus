//! The Rust side of MINUS.
//!
//! Phases 0–4 of `docs/REWRITE-PLAN.md`: the NDJSON control protocol, the
//! settings table, the `.env` writer, the control server, the fact store, the
//! tool registry and the shared tool loop. The conversation runtime, the speech
//! sidecar and the coding harness stay in Python for now; the point of this
//! crate is that every boundary it touches is already language-neutral.
//!
//! Everything here is a port of a named Python module, and the tests are parity
//! tests: frame shapes, error codes, settings defaults, `.env` editing rules,
//! the fact schema and the tool loop's accounting come from those modules rather
//! than from invention.

pub mod builtin;
pub mod chat;
pub mod client;
pub mod config;
pub mod control;
pub mod envfile;
pub mod llm;
pub mod protocol;
pub mod registry;
pub mod store;
