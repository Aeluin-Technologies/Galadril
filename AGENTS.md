* ALWAYS write industrial-grade, production-ready code with concise, precise Google-style comments in English.
* Restrict comments strictly to file headers, function docs, or complex logic blocks; do NOT summarize the code, instead explain WHY.
* ALWAYS write or update failing unit tests before writing production code (TDD).
* ALWAYS keep implementation details simple, clean, and legible (Keep It Simple, Stupid), but optimize hot paths for high throughput and low latency.
* Assume code runs under strict real-time constraints where heap allocations introduce latency jitter.
* Strictly prioritize zero-copy operations, reuse buffers, and minimize runtime memory allocations.
* ALWAYS use telemetry (logging, tracing, metrics) using OTLP.
* ALWAYS verify workspace state using `bazel test //...`; avoid language specific-tests.

# Language Rules

## Rust

* AVOID `panic!`, `unwrap()`, `expect()`, `unsafe`, and unchecked array indexing (`[]`). Use `?`, `get()`, or `match`.
* Handle every potential error using `Result` or log it appropriately via the `tracing` ecosystem.
* NEVER silently discard fallible operations with `let _ =`.
  * Propagate errors with `?` when caller-handled.
  * Use explicit `match` or `if let Err(...)` for custom logic.
* Avoid heavy cloning. Use explicit lifetimes, references, slices, or atomic references (`Arc`) for zero-copy semantics.
* Write flexible, idiomatic APIs using generic traits and bounds (e.g., `impl AsRef<Path>`, `impl Borrow<T>`).
* NEVER create `mod.rs` files; prefer `src/foo.rs` over `src/foo/mod.rs`.
* PREFER `#[expect()]` over `#[allow()]` for Clippy exceptions.
* PREFER `let` chains (`if let ... && ...`) over nested `if let` blocks.
* ALWAYS run before completing:
  cargo fmt --check
  cargo clippy --all-targets --all-features -- -D warnings

## Python

* ALWAYS use uv for execution and dependencies; NEVER invoke global python or pip. Then, run `bazel run //:gazelle_python_manifest.update` and `bazel run //:gazelle`. 
* PREFER async runtime using asyncio and uvloop for I/O bounds; offload CPU/blocking tasks to Executors to avoid blocking the event loop.
* ALWAYS minimize GC overhead using `__slots__`, `bytearray`, and `memoryview`.
* ALWAYS enforce strict type annotations (mypy/pyright compatible) without `Any`. Use Pydantic v2 for payload parsing.
* ALWAYS use structured domain exceptions or Result types. Log via structured JSON without leaking raw exceptions.
* ALWAYS run before completing:
    uv run ruff format --check
    uv run ruff check
