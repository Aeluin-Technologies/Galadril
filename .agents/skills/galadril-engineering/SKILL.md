---
name: galadril-engineering
description: >-
  Use for substantial Galadril code changes, refactoring, and engineering
  infrastructure. Covers bounded implementation, domain contracts and verification.
---

# Galadril engineering workflow

1. Read the applicable `AGENTS.md` files. Define the observable outcome, state
   owner, contract and callers. Use `$galadril-context-audit` for unfamiliar boundaries.
2. Write a behavior or gate test and observe it fail for the expected reason.
   Characterize existing behavior before extraction. Use
   `$galadril-property-tests` for parsers, invariants and transitions.
3. Preserve tenant context, authorization, idempotency, cancellation and error
   semantics across the change. Keep one phase per logical block and one
   authoritative implementation per invariant.
4. Run targeted Bazel tests, then the workspace and engineering gates. Introduce
   a deliberate violation for any new automated control. Report green, red and
   unavailable checks with the failing target or rule.

Consult [architecture and language conventions](../../references/ENGINEERING.md)
for modeling, API design and review. For concurrent work, read the
[ownership and concurrency guide](../../references/CONCURRENCY.md) and use
`$galadril-performance` when latency, throughput or allocations are at issue.
Use `$galadril-code-quality` for CRAP, duplication and mutation evidence.
