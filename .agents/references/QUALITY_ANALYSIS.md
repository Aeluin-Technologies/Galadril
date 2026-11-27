# Quality analysis

Crapper computes `CC² × (1 − coverage)³ + CC`, with coverage expressed as a
fraction. Use branch records when available and executable line records
otherwise. The gate distinguishes an instrumented function with zero hits
from a function with no mapped records. An emitted but never-loaded browser
chunk can supply genuine zero-hit points through its verified transformation. A
source record without executable points leaves its functions unmeasured. Rust `match` and `?` can increase
reported complexity. Inspect the function and measurement before interpreting
a score. Never lower a threshold or truncate the checked scope to pass.

Use [Dryer](https://github.com/unclebob/dryer) to review duplication in changed
paths. Start with similarity ≥ 0.8. Confirm the candidates encode the same
decision and share ownership before extracting. A bounded negative result
only speaks to the selected scope and configuration.

Use [Mutator](https://github.com/unclebob/mutator) after behavior and property
tests pass. Select bounded business-critical functions, wire the real Bazel
test command, and set a timeout, resource ceiling and cleanup policy. Review
survivors and distinguish equivalent mutants and failed builds from actual
test gaps. Run in an isolated worktree.

Use [Crapper](https://github.com/unclebob/crapper) through the repository's
pinned integration for reporting. Pin the version and grammars for reproducible
one-off analysis and record them beside the results.
