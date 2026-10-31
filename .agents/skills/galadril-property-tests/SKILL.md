---
name: galadril-property-tests
description: >-
  Use when Galadril code has testable algebraic contracts, parsers, encoders,
  canonicalizers, graph constraints or state transitions and generated
  counterexamples would be more valuable than selected examples. Not for
  UI end-to-end tests, fuzz campaigns or performance benchmarks.
---
# Property-based verification

1. Read the actual contract and existing test targets. Identify a property
   independently of the implementation: roundtrip, idempotence, invariant,
   oracle equivalence, ordering, or a state-machine property.
2. Specify the valid input domain, edge cases and invalid-input expectations.
   Generate valid structures directly; avoid near-total filtering by assumptions.
3. Prefer the PBT library already present in the affected codebase. For a new
   dependency, explain the property and request approval before installing it.
4. Run the property test before a proposed fix when feasible. Preserve the
   shrunk counterexample as a deterministic regression test.
5. Validate both positive and rejection paths. Do not assert a tautology that
   repeats the implementation or settle for `does not crash` if a stronger
   property is available.
6. Run the applicable Bazel test target and report seed/strategy settings,
   reproducibility, counterexamples and coverage limitations.

## Galadril candidates

- Registry semantic graph: valid inheritance DAGs remain acyclic; invalid
  ownership, references or resource kinds are rejected.
- ESKG: ordered edge kinds, endpoint kinds, tenant agreement and atomic
  ontology references remain valid after transformations.
- Pipeline: revision-pinned routing is deterministic for identical inputs;
  duplicated delivery does not repeat non-idempotent effects.
- Serialization: `decode(encode(x)) == x` only when the codec promises a
  lossless roundtrip; otherwise compare canonical representations.

Do not replace meaningful behavior tests with generated tests. See
`../../references/GALADRIL_CONTRACTS.md` for contract locations.
