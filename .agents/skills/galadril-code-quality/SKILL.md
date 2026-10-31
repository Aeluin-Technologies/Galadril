---
name: galadril-code-quality
description: >-
  Measure and improve changed Rust, Python, or TypeScript code using CRAP
  (complexity plus coverage), structural duplication, and mutation testing.
  Use for nontrivial refactoring, risky functions, code-quality reviews, or
  requests for Crapper, Dryer, or Mutator. Not for routine documentation edits.
---
# Galadril code quality: CRAP, duplication, mutation

## Assess before changing

1. Scope analysis to changed functions and their relevant callers. Record the
   baseline score, test suite, coverage source, tool version and configuration.
2. Inspect existing tools/coverage before running anything. `crapper`, `dryer`
   and `mutator` may bootstrap dependencies, create virtual environments, run
   language-specific tests, rewrite coverage snapshots or consume large resources.
   Do not fetch or install tools or mutate the project environment without
   authorization. Avoid broad scans on the monorepo by default.
3. `bazel test //...` is Galadril's authoritative workspace test gate. It does
   **not**, by itself, establish function-level coverage. Use credible coverage
   exported for the target under test; verify that the tool can map it correctly.
   If coverage is absent or incompatible, report CRAP as unavailable, not zero
   or a measured passing score. `--no-coverage` measures complexity only.

## CRAP: complexity and coverage

For each changed function f, compute or inspect:

    CRAP(f) = CC(f)^2 * (1 - coverage(f))^3 + CC(f)

where coverage is a fraction in [0,1]. Prefer `unclebob/crapper` with an
existing, trustworthy coverage report. It supports Rust, Python and TypeScript.
Read its version-specific README before running it. The `--changed`,
`--use-existing-coverage`, and `--threshold` options may help narrow analysis.
Do not assume the defaults integrate with Bazel, uv, pnpm or repository CI.

- Target **CRAP < 10** for new/modified nontrivial functions, following
  `ub-mode`; treat a score at or above 10 as a prompt for inspection.
- **CRAP > 30** is a high-priority complexity/coverage warning, not proof of a
  defect. Avoid increasing measured risk relative to the baseline.
- The `crapper --threshold 10` switch, if supported by the pinned version,
  flags values *above* 10; it does not strictly enforce `< 10` at equality.
  Review exact scores separately if the strict target matters.
- First add meaningful behavior or property tests where coverage is weak;
  simplify genuine branching complexity where coverage cannot resolve risk.
  Do not fragment cohesive code or silence errors merely to lower a score.
- In Rust, inspect the metric's counting conventions: `match` arms and `?`
  count toward its reported complexity. Record justified exceptions.

## Structural duplication: Dryer

For changed paths, review `unclebob/dryer` candidates with similarity >= 0.8
(the `ub-mode` review threshold; Dryer defaults may differ). Confirm that
matches duplicate domain decisions before extracting abstractions. Prefer one
source of truth for a genuine invariant; do not merge coincidentally similar
logic with different ownership, performance or tenant-security contracts.
Never claim the project is free from duplication merely because none was found
within the analyzed paths.

## Mutation testing: Mutator

Use `unclebob/mutator` on bounded, business-critical logic after behavior
and property tests pass. Start with scan/changed/selected-file modes when
supported. Review surviving mutants: some reveal weak tests, others are
semantically equivalent or arise from unsupported builds. Verify test commands,
resource limits and clean-up behavior before execution. Mutator's default
commands and coverage collection must not silently override the Bazel policy.
Do not infer a comprehensive quality guarantee from a mutation percentage.

## Verification and report

- Run the affected Bazel test targets while iterating, then required workspace
  and language gates from `AGENTS.md`; note any unavailable or blocked check.
- Report per changed function: CC, coverage source and fraction, CRAP before/
  after, retained exceptions; duplication candidates reviewed; mutation
  survivors and actions. Distinguish *not measured* from *passed*.
- Never automatically push, merge, deploy, install tools, discard edits or
  modify production data. Propose CI adoption only after a reproducible local
  run and review of the toolchain and its licensing.

## Tool references

- `ub-mode`: https://github.com/unclebob/ub-mode
- `crapper`: https://github.com/unclebob/crapper
- `dryer`: https://github.com/unclebob/dryer
- `mutator`: https://github.com/unclebob/mutator
