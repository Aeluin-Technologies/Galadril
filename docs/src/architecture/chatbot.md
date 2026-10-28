# Chatbot runtime and security boundary

See the [document retrieval architecture](library_retrieval.md) for chunked library
search that remains independent of Vision's entity embeddings and resolution.

## Decision

Use PydanticAI (MIT) for the agent loop, typed tools, multimodal inputs,
OpenAI-compatible providers and HTTP MCP clients. Run it as a private Python
microservice in `machine-learning/scribe` behind a workload mTLS proxy. Envoy
verifies public JWTs; Gateway consumes its verified identity. Keep the public API,
PostgreSQL conversations, S3 references, and authorization in Gateway.

Scribe aggregates administrator-approved aliases for official OpenAI Responses
and vLLM Chat Completions. Either provider, both or neither may be configured.
Every alias uses the same PydanticAI loop and permission-bound tools, including
GraphRAG and Monty calculations. Provider failures never trigger cross-provider
fallback. Official API keys never go to compatible endpoints.

Private `GET /models` and authenticated Gateway `scribeModels` expose aliases,
provider names and declared reasoning efforts, without credentials or internal
URLs. The catalog is configuration, not an upstream availability probe. Each
`ask` accepts `modelAlias` and optional `reasoningEffort`; Scribe validates both
before admission. Empty configuration returns an empty catalog and refuses runs
with 503. OpenAI response storage is disabled; Gateway remains the sole owner
of durable conversations. See [Scribe configuration](../../../machine-learning/scribe/README.md).

Use vLLM with the vLLM Metal plugin on Apple Silicon and vLLM on GPU servers.
Both expose the same OpenAI-compatible inference API; neither is a conversation
or authorization service. llama.cpp remains an optional compatible backend
without dedicated integration code. vLLM Metal runs natively on macOS 15+ in
its own Python 3.12 environment; Scribe uses the workspace's Python 3.13.
Configure model aliases server-side. No model weights
are loaded into Gateway. Bound context, output tokens, tool calls and concurrent
runs; benchmark the selected quantized model on the target Mac before claiming
latency or memory performance.

Agno/AgentOS provides more built-in session endpoints, but adopting its separate
session database would duplicate Gateway's permission and revision contracts.
LangGraph provides durable graph execution, but its production Agent Server
has separate deployment/licensing considerations. PydanticAI's dependency
injection lets all privileged tools remain behind our existing application
services without introducing a second IAM or storage model.

Sources consulted on 2026-10-04:

- https://github.com/pydantic/pydantic-ai
- https://ai.pydantic.dev/mcp/client/
- https://ai.pydantic.dev/testing/
- https://github.com/ggml-org/llama.cpp/tree/master/tools/server
- https://docs.vllm.ai/en/latest/getting_started/installation/
- https://docs.vllm.ai/projects/vllm-metal/en/stable/installation/
- https://vllm.ai/blog/2026-09-22-vllm-metal-v0-28-0
- https://docs.agno.com/agent-os/using-the-api
- https://docs.langchain.com/langsmith/deploy-standalone-server

## Invariants

1. Tenant and actor come from the authenticated request, never a model argument.
2. Scribe has no PostgreSQL, SpiceDB, S3 or administrative credentials.
3. A generation-scoped, expiring capability authorizes only tool invocation.
   Every invocation rechecks active identity, conversation permission and current
   resource permissions through Loth (SpiceDB and Cedar). A capability cannot
   grant a permission its actor lacks. Terminal generations invalidate it.
4. PostgreSQL access uses Gateway's non-superuser, NOBYPASSRLS role and
   transaction-local tenant settings. AGE traversal uses the existing bounded,
   tenant-scoped graph adapter; results are filtered per node, edge and evidence.
5. Tools expose typed search, graph and causal operations, never arbitrary SQL/Cypher.
   Ontology labels are returned as data; unattributed graph properties are redacted.
   Client ontologies do not require hard-coded entity or relationship labels.
6. Attachment URLs are created only after current resource permission and S3
   ownership checks. Persist object references, never presigned URLs.
7. Only administrator-configured model and MCP endpoints may be contacted.
   Delegation credentials go only to Gateway, never to external MCP servers.
   Internet and sandbox tools receive no database or application credentials.
8. Prompts, reasoning, tool bodies and signed URLs are excluded from telemetry.
   Export metadata-only OpenTelemetry traces, metrics and structured logs through OTLP.
   Framework payload and exception spans are disabled.
9. Historical answers can contain previously authorized evidence. Recheck
   evidence visibility before feeding such history to a new run; until complete
   provenance exists, history sharing is restricted to its original actor.
10. Amarth may use every required tenant input, regardless of the requesting
    user's raw-data visibility. Access to a root causal result follows that
    entity's view permission. Publish bounded root statistics, without input
    identities, source features, raw observations or the complete causal chain.
    Historical causal results are rechecked against root access independently
    of raw evidence permissions.

## Conversation transitions

`idle -> pending -> completed | failed`. Reservation and user message insertion
are atomic. At most one generation owns a conversation. Message edits preserve
immutable revisions and cannot race an active generation. Completion persists
the assistant message before releasing the reservation.

Generation is owned by a server worker, independently of the WebSocket. Slow or
disconnected subscribers cannot block persistence. Reconnection reads persisted
generation events with a monotonically increasing cursor, then polls for more;
it never restarts inference. Events and terminal messages share tenant-scoped
foreign keys and forced RLS. Reasoning is not persisted.

Threads are independent conversations with independent authorization. Forking
must copy only authorized messages and attachment references in one transaction;
editing a message must not silently reinterpret earlier assistant answers.

Process-crash recovery is distinct from client disconnection: interrupted runs
must become explicitly failed or be resumed by an external durable execution
backend. Never automatically replay mutating tools. Gateway control-plane write
tools need explicit action policies and idempotency before being enabled.

## Verification

Write regressions before implementation: mixed tenant/actor attempts, forged and
expired capabilities, revoked access, denied evidence, arbitrary query rejection,
unknown models, oversized inputs, truncated upstream streams, slow/disconnected
subscribers, replay cursors, message revisions, and S3 attachment isolation.
Exercise the framework's real tool loop with a deterministic test model and real
PostgreSQL under the application role. Run all tests through Bazel.

The pipeline lifecycle E2E ends with the real private runtime and an independent
OpenAI protocol fixture. Compare its tool-derived answers with PostgreSQL,
check users with and without evidence access, root-only causal access, ABAC,
revocation and cursor replay after client disconnection. Model quality and Mac
performance require separate evaluation using the selected real model.


## Specialist agents and local tools

Saved specialists are personal tenant-scoped records with a mission, optional project
scope and registered Skill IDs. Selection is explicit per conversation (up to eight);
Gateway checks current ownership, project access and transitive model/tool compatibility
before reserving a generation. Scribe receives an inline delegated team, not a trusted
implementation identifier. Specialists share the same scoped tools and global run
budgets; the orchestrator synthesizes their attributed reports. This does not schedule
background monitoring jobs.

The default public web-search tool uses Gateway's configured SearxNG service, included
in the default Compose deployment. Generate the local search secret with
`uv run infrastructure/dev/galadril.py init`; no paid search API key is required. Model
inference still uses the selected provider. OpenAI's billed native search requires an
explicit `SCRIBE_WEB_SEARCH_PROVIDER=openai` and matching Gateway configuration;
SearxNG failure never selects it automatically. Respect upstream engine usage limits.

Python execution publishes redacted code, bounded output and one PNG as durable typed
activities. The UI shows code/output in a disclosure and can replay previous executions.
The default Monty interpreter remains available without Docker access or service keys;
its reviewed numerical/plotting bridge supports a subset of NumPy/pyplot operations,
including single-axes `fig, ax = plt.subplots()`, axes labels and PNG saving.
Native Figure/Axes internals and arbitrary filesystem paths remain inaccessible.

Full CPython and temporary approved libraries are an explicit deployment option.
It adds one persistent controller (256 MiB memory limit). Workers exist only during
executions and are removed afterwards; each is limited to 1 GiB and one CPU, with
at most two simultaneous executions by default. These are ceilings, not reserved
resources. Monty uses no additional sandbox container.

To enable the full CPython option:

- Include `infrastructure/docker/python-sandbox.yaml` as an additional Compose file
  and select the `python-sandbox` profile. Set `GALADRIL_PYTHON_SANDBOX_IMAGE` and
  `GALADRIL_PYTHON_WORKER_IMAGE` to published, digest-pinned images from the matching
  release. They are separate Bazel OCI publication targets.
- Install a supported isolated runtime on the Docker host. Production configuration
  defaults to `runsc` and rejects plain `runc`; Kata is another operator-selected
  runtime. Docker Desktop without these runtimes may use `runc` only with explicit
  development mode. Do not describe that as gVisor/Kata proof.
- Scribe reaches the controller only through a private shared Unix socket (directory
  mode 0700, socket 0600), with compatible service UIDs. No API key or public network
  listener is required. Only these trusted services receive the IPC volume.
- Only the controller receives the Docker socket. Access to the Docker daemon is
  privileged even through a read-only bind; deployment remains opt-in and should use
  a dedicated daemon/host. User code gets neither that socket nor host mounts,
  application credentials or networking.
- Each unprivileged worker has a read-only root, dropped capabilities, resource/time
  limits and a bounded temporary workspace. It creates a fresh uv virtual environment
  and installs requested libraries offline from hash-pinned wheels built into the
  image. The closed policy permits NumPy, Matplotlib, pandas, SciPy, SymPy, Pillow
  and scikit-learn, plus their reviewed locked dependencies. No arbitrary package
  names, versions, repositories, source builds or install flags enter the installer.
  Destroy the container after every result/failure; a scoped janitor reaps orphaned
  workers after controller crashes.

The real Docker integration test proves installation, a PNG, fresh environments,
blocked network/host-file access and timeout cleanup with the development runtime on
BuildBuddy. Production runtime isolation still requires a deployment test with the
configured gVisor/Kata host; a configuration unit test is not that proof.
