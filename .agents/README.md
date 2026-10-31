## Available skills

| Skill | Invoke when | Do not invoke for |
| --- | --- | --- |
| `galadril-engineering` | Nontrivial code implementation/refactor | Read-only questions or trivial edits |
| `galadril-context-audit` | Unfamiliar module or audit mapping | Finding/rating vulnerabilities |
| `galadril-property-tests` | Algebraic invariants and counterexamples | General UI E2E tests |
| `galadril-security` | Tenant/auth/RLS/RAG/tool boundary reviews | Routine lint |
| `galadril-observability` | New operational signal or blind spot | Direct profiling |
| `galadril-ontology` | Registry, ESKG, semantic revisions | Unrelated SQL optimization |
| `galadril-distributed-pipelines` | Kafka/FastStream/Ray execution and replay | Local pure functions |
| `galadril-performance` | Measured hot paths and resource usage | Speculative optimization |
| `galadril-ai-evaluation` | Model, RAG, extraction or ranking changes | Simple API wiring |

## Composition and authority

- `AGENTS.md` (root and scoped) remains authoritative. Skills are
  *conditional workflows*, not replacements for repository rules.
- The minimal integration snippet is in `.agents/AGENTS-addendum.md`.
  Review and append it to root `AGENTS.md` if desired. It is not loaded
  automatically while stored separately.
- `$galadril-context-audit` may precede `$galadril-security` for unfamiliar
  code. `$galadril-property-tests` supports `$galadril-engineering` or
  `$galadril-ontology` when there is an actual algebraic property.
- Avoid automatically chaining unrelated skills. Prefer one lead workflow
  and one specialist when warranted.
- Do not merge, push, deploy, delete worktrees, access production secrets,
  install new dependencies or alter live infrastructure without authority.
- This package deliberately includes no third-party original `SKILL.md`
  texts, no installed quality-gate executables and no network scripts.
