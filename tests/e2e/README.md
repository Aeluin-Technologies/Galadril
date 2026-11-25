# E2E contracts and failure isolation

Use the authenticated BuildBuddy configuration. The lifecycle uses deterministic
local model fixtures and live HTTP, authorization, persistence, graph ranking
and pipeline orchestration. See [CI workloads and measurements](CI.md) for lane
membership, cache policy and reproduction commands.

Browser actors authenticate again before cookies are captured, so pipeline
duration and earlier browser renewals cannot invalidate a reused snapshot.
Production expiration and authentication limits are unchanged.

## Real-service scenarios

| Test | Contract and independent oracle |
| --- | --- |
| `test_authentication.py` | Real Kratos/Hydra sessions, passkey assurance, tenant claims, signed expiration; an unauthenticated fixture actor cannot invent a JWT. |
| `test_proxy_startup.py` | Shipped read-only Compose configuration mounts, trusted TLS and private proxy endpoints, without loading application models. |
| `test_s3_notifications.py` | A real conditional S3 copy reaches Kafka through the shipped relay; independent of Vision startup. |
| `pipeline_authorization_test` | Missing, malformed, expired and tampered credentials fail closed. GraphQL inventory requires an explicit disposition for new fields. IAM and conversation CRUD use the real Gateway, and revoked administrators lose access. |
| `pipeline_publication_test` | Real ontology and pipeline revisions, review, denied publication, runtime bindings and retirement. |
| `gol_lifecycle_test` | Real Registry/Gateway with Chromium for GOL authoring and authorized published projections. |
| `pipeline_lifecycle_test` | Vision waits for publication without restarting. One Gateway upload goes through S3, Kafka, Intake, resolution and persistence. Verifies upload scope/path denials, derived access and isolation, lineage and caller-linked OTLP traces. |
| `pipeline_empirical_test` | Retail identity, access oracles, randomized-trial causality and retrieval over the live identities. The dedicated benchmark runs all 240 retail and 205 trial rows. |
| `empirical_causal.py` | The pinned randomized trial and independently computed uncertainty/effect are compared with live Amarth results, under per-principal access. |
| `retrieval_lifecycle.py` | Documents bind to live ESKG customer/source-row identities; public search exercises BM25, dense, hybrid and associative modes; outsider results stay empty and document indexing leaves ESKG state/vector counts unchanged. |
| `pipeline_assistant_test` | A fresh causal cohort drives both assistants. Chatbot verifies WebSocket disconnect and ordered replay, scoped search/graph/causal evidence, and persisted provenance. |
| `pengolo_execution_test` | Native checkpoint and real MCP transport on an empty retrieval schema. |
| `pengolo.py` | Draft/idempotency boundaries, project hierarchy and role changes, immutable upload/download and revocation, optional private-graph warning without model evidence, temporary storage/expiry and cancellation. Role revocation precedes browser isolation checks. |
| `dashboard/live.spec.ts` | Real authenticated cookies and catalog response, compatible default selected in the DOM, upload and graph selection, detached inference across reload, terminal completion, mobile layout, shared project/file access, live role grant/revoke and closing a now-forbidden preview. |

## Browser interaction contracts

`test_dashboard.py` uses the same compiled Nuxt runtime and real Ory authentication.
Its Gateway double/page routes isolate UI behavior; they do not prove Gateway
authorization, inference, vector retrieval, or distributed durability.

| Scenario in `dashboard/` | Required behavior |
| --- | --- |
| Anonymous protection | Protected pages redirect; email is the only initial login field. |
| Password/passkey/logout | Transitions preserve the document; reload keeps the session; logout protects it. |
| Enterprise OIDC | Real Polis login and passkey enrollment complete. |
| Member administration | Navigation and direct access both deny ordinary members. |
| Organization/people | Organization opens first; destructive actions require confirmation. |
| SSO consent | Email normalization and detected configuration require explicit consent. |
| People reload | Rotating refresh feedback, completion announcement, and unchanged document identity. |
| Permission round-trip | Visual/code rules preserve opt-in and review. |
| Guided permissions | Choices, filtered access map and UTC rules remain in place. |
| Administration errors | Recovery is actionable and private backend detail is absent. |
| Mandatory passkey | Enrollment opens immediately in a modal and preserves the active page. |
| Session renewal | Only active visible clients renew; protected cookies and identity binding persist. |
| Expired access token | Signed session restores during navigation and reload. |
| Narrow administration | Content remains scrollable and reachable. |
| Directory pagination | Server pages append; fuzzy results are not incorrectly filtered locally. |
| Pengolo catalog before composer | A preloaded catalog also survives drawer-to-workspace navigation and reload. |
| Pengolo drafts/dynamic models | Local draft causes no inference, incompatible models are hidden, identity invalidation closes private previews, and one explicit send persists. |
| Pengolo history | Ten-message pages, attachments and rich Markdown tasks persist without unnecessary complete-history requests. |
| Pengolo interaction | Planning, Ask User answers, further text/tool rounds, and completion remain ordered and usable. |

## Fast fixture and regression checks

| Test module | What its result establishes |
| --- | --- |
| `test_support.py` | Authentication budgets, bounded subprocess cancellation/reaping, teardown preserving the original failure, readiness and publication/configuration/OTLP contracts. |
| `test_chatbot.py` | Replay refuses missing/duplicate/late events, terminal failure stops polling, mutation responses are typed, and optional graph denial requires warning plus no model evidence. |
| `test_chat_model.py` | Cached local fixture drives production Scribe tool loops, scoped arguments, calculator-only requests and library selection/read. |
| `test_causal_fixture.py` | Identity embeddings separate decoys and empirical customers; invalid evidence is rejected; causal workflow fixtures remain tied to source rows. |
| `test_empirical_data.py` | Source hashes, selected retail rows and independent randomized trial estimates are pinned. |
| `test_scenario.py` | Every source row appears once, with the intended principal, cancellation kind and independent visibility set. |
| `test_retrieval_benchmark.py` | Recall counts unique relevant documents; cached local models and recognized facts share grounded source evidence; associative retrieval does not regress against its fixed dense baseline. |
| `test_official_memory.py` | Complete pinned upstream package on 32 fixed questions: dense, HippoRAG and explicit ESKG bridges, with cached local embeddings/reranking; eight source clusters are reported rather than claiming 32 independent samples. |
| `test_upstream_retrieval.py` | Official upstream graph kernel ranking, scoped candidates, explicit live ESKG bindings and missing-binding rejection. This is not a full official HippoRAG service/LLM benchmark. |
| `test_chunking_benchmark.py` | Exact source spans and embedding profiles are preserved across fixed/structural/experimental causal pooling. Eight source-evidence queries do not establish statistical improvement or complete late-chunking support. |
| `test_hotpot_benchmark.py` | Forty attributed HotpotQA distractor questions (20 bridge/20 comparison), two support documents, cached local embeddings/reranking, Recall@5/joint support and paired-comparison math. It does not exercise the live Gateway or compare HippoRAG on HotpotQA. |

Keep expected authorization and retrieval results independent from the service
being checked. Do not accept an outage as a permission denial, increase a timeout
to hide a deterministic assertion, or rename a cached-model measurement as a live
model benchmark. Browser failures retain screenshots and traces; the model
selection failure includes DOM options and the catalog store state.
