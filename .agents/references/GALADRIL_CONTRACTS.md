# Galadril contract map (reviewed 2026-10-31)

This is a **routing index**, not a substitute for reading current source.
Verify paths and active implementation whenever applying a skill. Some docs
specify proposed or not-yet-deployed behavior.

| Concern | Authoritative starting point | Key constraints to verify |
| --- | --- | --- |
| Repository-wide coding | `AGENTS.md` | TDD, Bazel, OTLP, Rust/Python rules, side effects |
| Dashboard | `front/AGENTS.md`, `front/dashboard/AGENTS.md` | Nuxt/Vue, UI package boundaries, accessibility, Bazel |
| Ontology | `docs/src/architecture/ontology.md`, `platform/registry/` | Registry sole semantic authority; immutable revision; tenant binding |
| ESKG | `docs/src/architecture/eskg.md`, `platform/vision/` | Structural labels, permitted relations, graph tenancy, ontology_ref |
| Security | `docs/src/architecture/authorization.md` | SpiceDB, Cedar, PostgreSQL RLS, transaction-local tenant scope, outbox |
| Proxy trust | `docs/src/architecture/zero_trust.md`, `infrastructure/auth/` | Workload identity != tenant authorization; verified boundary |
| Durable pipeline | `platform/pipeline/README.md`, `docs/src/architecture/pipeline_routing.md` | Kafka/FastStream/Ray roles, pinned route, no implicit multi-input joins |
| Vision worker | `platform/vision/README.md`, `platform/vision/` | FastStream context, Ray actors, OTLP, identity-resolution writer ownership |
| Search and retrieval | `docs/src/architecture/library_retrieval.md`, `docs/src/architecture/retrieval_audit.md` | Current authorized evidence, immutable source revision, access invalidation |
| Tool capabilities | `docs/src/architecture/capabilities.md`, `docs/src/architecture/official_plugins.md` | Discovery != permission; approvals; built-in Scribe workflow skills != Codex skills |
| ML | `machine-learning/README.md`, `machine-learning/galadril-inference/`, `machine-learning/scribe/`, `machine-learning/eru/` | Reproducibility, authorization, model evaluation |
| Database | `database/README.md`, `database/tests/` | PostgreSQL + AGE + TimescaleDB + pgvector; RLS roles |

## Verification convention

- `AGENTS.md`: workspace gate `bazel test //...` plus language-specific
  checks, when applicable. During iteration use the narrow affected Bazel
  targets, then report whole-workspace results honestly.
- Existing security regression target documented in
  `docs/src/architecture/authorization.md`:
  `bazel test //platform/vision/tests/security:authorization_defense_in_depth`.
  Verify target still exists before running; do not mistake one suite for a
  proof of all isolation properties.
- Do not invent status for unimplemented or planned services. Distinguish
  documented intended architecture from tests and runtime evidence.

## External baselines

- Codex skills: https://developers.openai.com/blog/skills-agents-sdk
- Agent Skill authoring: https://developers.openai.com/plugins/build/skills
- OWASP multi-tenant security: https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html
- OTel messaging: https://opentelemetry.io/docs/specs/semconv/messaging/
- OTel context and sensitive baggage: https://opentelemetry.io/docs/concepts/context-propagation/
