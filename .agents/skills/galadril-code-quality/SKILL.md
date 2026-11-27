---
name: galadril-code-quality
description: >-
  Use for Galadril CRAP, structural duplication, mutation testing, or a
  review of risky Rust, Python or TypeScript functions.
---

# Galadril code quality

1. Identify changed functions and callers. Record tool versions, Bazel
   invocation, source scope and existing evidence. Run targeted tests first.
2. Use the [Bazel engineering gate](../../../build/quality/README.md) and its
   Crapper adapter with real LCOV from the same revision. CRAP below 10 is the
   objective. A score above 30 or missing function evidence blocks the gate.
   Review scores from 10 through 30. Record measured zero, missing evidence
   and declarations without runtime bodies separately. For reproduction, repeat
   collection with uncached test results and compare per-function measurements.
3. Read the [quality analysis reference](../../references/QUALITY_ANALYSIS.md)
   when investigating scores or using Dryer or Mutator.
4. Improve assertions, properties or genuinely complex design. Preserve API
   behavior and avoid hiding risk by splitting cohesive functions or suppressing
   diagnostics.
5. Report complexity, coverage source and fraction, CRAP, reviewed duplicate
   candidates, surviving mutants and verification limits as applicable.
