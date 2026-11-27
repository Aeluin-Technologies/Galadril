# Domain design and review

- Identify each bounded context by its model, invariant owner and public contract.
  Keep domain rules free of transport, persistence, framework and plugin
  implementation dependencies. Application services coordinate ports, and
  adapters translate transport and persistence representations. Composition roots
  bind implementations. Plugins declare capabilities through extension contracts
  with explicit versioning and ownership.
- Parse at the trust boundary. Decode into a wire DTO, authorize trusted context,
  and construct the domain value through a validating factory. Preserve which
  input was invalid in a safe, structured error. Avoid handing raw strings or
  dictionaries to code that assumes invariants already hold.
- Model IDs, tenant scope, units, revisions and state with the narrowest useful
  types. Use Rust private fields and `TryFrom`, Python frozen value objects with
  validating constructors, and TypeScript classes or branded values created
  through a validating factory. Use discriminated unions and exhaustive handling
  for closed state sets. Introduce typestate when operations differ by state.
- Give functions cohesive responsibilities. Separate initialization, parsing,
  authorization, processing, persistence, observation and return with blank
  lines when phases are present. Extract a helper around an independently
  meaningful operation. Consolidate actual duplicate rules and keep unrelated
  responsibilities independent.
- Document public contracts in English, including ownership, errors, observable
  side effects and invariants. Use Rustdoc sections and executable examples,
  Google-style Python docstrings, and TSDoc for parameters and results that
  need explanation. Explain ordering and failure-handling decisions where code
  alone does not convey why they are necessary.
- Rust APIs should borrow where ownership does not transfer, use concrete
  domain types at semantic boundaries, and use generics when multiple
  representations are actually supported. Handle risk-prone operations with
  `Result`, `Option` and checked access. Keep generated or foreign bindings at
  the edge and preserve build-target visibility.
- Python adapters parse untrusted payloads with Pydantic v2. Use `object` and
  narrow it at the boundary, with protocols for typed capabilities. Keep
  untyped dependencies behind typed adapters. Prefer immutable values and
  select `slots`, buffers or views when appropriate to the allocation profile.
- TypeScript uses `unknown` at untrusted boundaries, validated constructors
  and discriminated unions. Keep type imports explicit, prefer immutable
  values and handle rejected promises. Preserve typed Vue props, events and
  slots. Validate Vue templates with Vue TSC and TypeScript files with their
  owning project configuration.
- Review change intent, API behavior, dependency direction, error paths,
  concurrency and test quality. Ask whether tests would fail on a meaningful
  regression. Preserve existing tests and lints. Review any scoped compiler
  exception against its concrete invariant and evidence.

## Source guides

- [Google Rust style](https://google.github.io/styleguide/rustguide.html)
- [Microsoft Pragmatic Rust Guidelines](https://microsoft.github.io/rust-guidelines/)
- [Google Python style](https://google.github.io/styleguide/pyguide.html)
- [Google TypeScript style](https://google.github.io/styleguide/tsguide.html)
- [Microsoft TypeScript compiler coding guidelines](https://github.com/microsoft/TypeScript/wiki/Coding-guidelines)
- [Google engineering practices](https://google.github.io/eng-practices/)
