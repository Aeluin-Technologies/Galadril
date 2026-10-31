---
name: galadril-engineering
description: >-
  Use for nontrivial production code changes, bug fixes or refactoring in Galadril
  that require a bounded implementation and verification workflow. Not for
  read-only research, trivial edits or architecture-only audits.
---
# Galadril engineering workflow

The repository `AGENTS.md` and its nested variants remain authoritative for
language, Bazel, telemetry and security conventions. Do not restate or weaken them.

## Work, then improve

1. Establish the requested observable outcome, affected ownership boundary and
   the smallest coherent change. Inspect callers and contracts before editing.
2. Check the domain shape and whether existing APIs can express the change.
   Avoid new shared mutable state, speculative abstractions and compatibility
   layers with no demonstrated consumer.
3. Write or adjust a behavior test first; run it and observe a failure for the
   expected reason. For a behavior-preserving refactor, characterize existing
   behavior first. Do not fabricate an artificial failing test.
4. Implement the smallest fix. Preserve tenant scope, error propagation,
   idempotency and existing public contracts. Fix the root cause, not just
   the visible symptom.
5. Run targeted Bazel tests while iterating, then the workspace gate required
   by `AGENTS.md` (`bazel test //...`). Run relevant language checks as specified
   there. Distinguish completed, failed and unavailable checks.
6. Refactor only after behavior is pinned; measure rather than assume gains in
   hot paths. When useful, employ property or mutation tests through their
   own task-specific workflows; do not invent mandatory CRAP/duplication
   scores or install tools automatically.

## Safety and report

- Keep changes reviewable; never silently delete a worktree, discard edits,
  merge, push, deploy or mutate production data.
- Report: behavior changed, tests added, commands/results, residual risks and
  unverified assumptions. A short meaningful log suffices; no exhaustive
  narration is needed.
- If you encounter an unfamiliar module or a trust boundary, use
  `$galadril-context-audit` before claiming to understand it.
