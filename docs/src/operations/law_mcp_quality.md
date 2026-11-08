# Law and MCP quality evidence

Measured on 8 November 2026. This is a bounded review of the new Python MCP
implementation, legal instruction workflow and generic task graph, not a quality
score for the entire monorepo. Model behavior tests use synthetic fixtures and
PydanticAI `FunctionModel`, not live legal advice or a real private dossier.

## Tools and coverage

- Crapper: `6fd92c4c9bcc56efb64a2f8743d5a2993bcfd42d`.
- Mutator: `a5a0cb803b66e0cdb8bf471b3b08af701d424a41`.
- Dryer: `66ff6d21a42c04afcad89c78a80066176d1294b0`.
- Tests and coverage use Bazel with BuildBuddy, `--config=ci --config=release`,
  remote execution and no local fallback. Credentials remain in a private
  Bazel configuration file and are absent from reports.
- Python toolchain coverage is enabled. LCOV is collected with
  `--combined_report=none --experimental_fetch_all_coverage_outputs`,
  `--instrument_test_targets` and instrumentation for `platform/mcp`,
  `services/mcp`, `plugins` and `machine-learning/scribe`.
- Per-test LCOV records are merged by source path: line hits are added and
  branch hits are combined. A dependency's zero-hit record must not replace
  coverage from the test that actually exercises it.
- Crapper uses existing coverage, preferring branch coverage inside functions.
  It never starts a language-specific test runner. Dryer uses threshold 0.8
  with its default minimum sizes; it found no candidates in the measured paths.

The [per-function CSV](law_mcp_quality.csv) covers 71 functions in the MCP core
and service, `law/instruction.py`, `law/tools.py`, `law/piste_tools.py`,
`scribe/workflows.py` and `scribe/workflow_execution.py`. Every measured score is
strictly below 10. The maximum is 9.9954 (`TaskSpec.scoped`), displayed as 10.0 by
the CLI's rounding. This does not mean every function or defensive branch has
100% coverage. Existing large Runtime functions, Rust and frontend code are not
included in this CRAP measurement.

Initial implementation entry points were split by responsibility and retested:

| Responsibility | Initial CC / branch coverage / CRAP | Final main check CC / coverage / CRAP |
| --- | --- | --- |
| OAuth signature, identity and resource | 18 / 56.2% / 45.1 | 6 / 100% / 6.0 |
| Official citation identity and quotation | 17 / 20% / 165.0 | 6 / 100% / 6.0 |
| Instruction coordinator | 14 / 55.8% / 30.9 | 6 / 55% / 9.3 |
| Native MCP authorization | 8 / 55.6% / 13.6 | 6 / 71.4% / 6.8 |

These are responsibility comparisons, not identical function bodies. Header
parsing, key loading, source identity, quotation checks and refinement policy
have their own entries in the CSV. The baseline is the first implementation,
not the pre-feature `main` branch.

## Mutation audit

Mutation runs use isolated source copies, one worker and an explicit Bazel test
command with timeout factor 30. Every failing invocation was checked for a
completed failing test, rather than a build, authentication or timeout failure.

- Citation verifier: 22 executed mutants detected; no survivor. Two sites have
  no executable-line hit in Mutator's mapping, although both provider paths are
  exercised by the tests.
- MCP URL policy, JWT identity/resource checks and native grant verification:
  25 detected, two equivalent survivors, one site not executed by coverage
  mapping. Other key-cache/loading methods were outside this mutation run.
- The two equivalent survivors change the minimum token-string length from
  one to zero or exclude one-character strings. Such strings cannot form a
  JWT; the decoder still rejects them before any authority is accepted.

The initial campaigns exposed missing assertions for malformed source records,
gateway failure responses containing otherwise valid identity data, exact size
limits, token expiration arithmetic and allowed boundary values. Tests were
added and the actionable survivors were rerun. The result is 47 detected
non-equivalent mutations, not a repository-wide mutation score.

## Behavioral evidence and limits

Seventeen affected Bazel targets passed across the final targeted runs:
Gateway, MCP delegation, Scribe Runtime/teams/plugin integration/document tools,
task graphs, dynamic instruction, plugin templates/ontologies/PISTE/MCP,
frontend authentication and frontend tool typechecks. Tests cover independent
defenders, private history exclusion, exact artifact paging, evidence scope,
budget reservations, replay accounting and research-question refinement.

Useful BuildBuddy snapshots:

- [Targeted cross-service checks](https://app.buildbuddy.io/invocation/d5c7829c-05cd-409b-bc0c-56ab00e0117b).
- [Gateway authorization and delegation](https://app.buildbuddy.io/invocation/ed26d9c5-57d9-4e32-90b3-0d4cbd6b385b).
- [Coverage and evidence-scope checks](https://app.buildbuddy.io/invocation/9a73080d-b6dd-4b46-b51f-a01113843f0f).

The required [workspace gate](https://app.buildbuddy.io/invocation/4fc21520-fa07-4691-a235-52c4f26ede9a)
did not pass: 51 targets could not build under forced remote execution, including
actions explicitly requiring local execution, and the host ran out of disk
space. Three application test failures in that run were subsequently corrected
and passed targeted reruns. Global CI success is therefore not established.

Workspace Ruff passes. The changed Rust modules pass the nightly formatter;
workspace formatting also reports unrelated existing files. The full Clippy run
was attempted but exhausted disk space. Both required Gazelle commands built
remotely but their generated Linux executables could not run on the macOS host.

Read-only production PISTE contract checks returned HTTP 200 for Judilibre
health, Légifrance decision consultation, JORF consultation, jurisprudence search
and dated code-article search. Credentials and response bodies were not logged.
This does not prove a full live Pengolo investigation or an external ChatGPT
OAuth connection. The latter still requires the deployment's exact callback,
public issuer and configured Cloudflare tunnel.
