# Galadril engineering standard

- Follow the closest `AGENTS.md`. Scope changes to the requested behavior and preserve concurrent work.
- Model bounded contexts with DDD, Clean Architecture and Ports & Adapters. Keep dependency direction inward and isolate plugin implementations behind host-owned ports.
- Parse external data into validated domain types. Use Rust newtypes, Python value objects, TypeScript branded types or classes, enums and typestate where they represent real invariants. Reject explicit and implicit `Any`/`any`, unchecked assertions and type-check suppressions.
- Write idiomatic code with business names and cohesive modules. Separate logical phases with blank lines. Document public contracts, invariants and decisions in technical English using Rustdoc, Google docstrings and TSDoc. Keep comments to file headers, API docs and complex logic.
- Prefer exclusive ownership, borrowing, reusable buffers and measured allocation reductions. Reserve shared ownership for independent owners. Use bounded queues, quotas, cancellation and supervision. Preserve parallelism and use the simplest correct synchronization primitive.
- Use fallible Rust APIs and propagate or handle every `Result`. Avoid `unsafe`, panics, unwraps and unchecked indexing. Keep Rust modules flat, forbid new `mod.rs` files, use let chains where clear, and prefer specific `#[expect]` for reviewed lint exceptions.
- Use uv for Python. Refresh Gazelle metadata when imports or dependencies change. Type public interfaces strictly and parse payloads with Pydantic v2 at adapters. Offload blocking work from async event loops. Emit structured domain errors and bounded, privacy-safe OTLP signals at meaningful boundaries.
- Write a failing behavior or gate test first. Preserve existing behavior with characterization tests during extraction. Use property tests for invariants, parsers and state transitions.
- Run targeted Bazel checks and `bazel test //...`. Run the [engineering gates](build/quality/README.md) with the pinned Bazel toolchains and BuildBuddy configuration. Use Bazel coverage artifacts with verified source identity. Preserve existing tests, controls and thresholds. Reject unjustified deletions, exclusions and suppressions.

Use `$galadril-engineering` for implementation, `$galadril-context-audit` for unfamiliar boundaries, `$galadril-code-quality` for CRAP, Dryer and Mutator, and `$galadril-property-tests` for invariants. Apply the relevant security, ontology, distributed-pipeline, observability, performance and AI-evaluation skills at their boundaries.
