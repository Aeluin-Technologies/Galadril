# Chatbot runtime and security boundary

## Decision

Use PydanticAI (MIT) for the agent loop, typed tools, multimodal inputs,
OpenAI-compatible providers and HTTP MCP clients. Run it as a private Python
microservice in `machine-learning/scribe`. Keep the authenticated public API,
PostgreSQL conversations, S3 references, and authorization in Gateway.

Use native llama.cpp with Metal on Apple Silicon. vLLM is an interchangeable
OpenAI-compatible inference backend for GPU servers; it is not a conversation
or authorization service. Configure model aliases server-side. No model weights
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
5. Tools expose typed search and graph operations, never arbitrary SQL/Cypher.
   Ontology labels and properties are returned as data, not executable prompts.
   Client ontologies do not require hard-coded entity or relationship labels.
6. Attachment URLs are created only after current resource permission and S3
   ownership checks. Persist object references, never presigned URLs.
7. Only administrator-configured model and MCP endpoints may be contacted.
   Delegation credentials go only to Gateway, never to external MCP servers.
   Internet and sandbox tools receive no database or application credentials.
8. Prompts, reasoning, tool bodies and signed URLs are excluded from telemetry.
   Export metadata-only OpenTelemetry traces and metrics through OTLP.
9. Historical answers can contain previously authorized evidence. Recheck
   evidence visibility before feeding such history to a new run; until complete
   provenance exists, history sharing is restricted to its original actor.

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
