# Galadril Scribe

Scribe runs PydanticAI agents behind Gateway's authenticated GraphQL API.
Inference is delegated to an administrator-configured OpenAI-compatible server;
Gateway owns conversations, generation events, attachments and authorization.
Scribe holds no database, SpiceDB or S3 credentials. The LaTeX report generator
and embedded mistralrs runtime have been removed.

## Running the service

Start an inference server separately, then configure Scribe through environment
variables or a deployment secret store:

```sh
export SCRIBE_GATEWAY_TOOLS_URL='http://127.0.0.1:8082/internal/chat/tools'
export SCRIBE_MODELS='{"local":{"model":"<served-model-id>","base_url":"http://127.0.0.1:8000/v1"}}'
export SCRIBE_DEFAULT_MODEL='local'
bazel run //machine-learning/scribe/python/galadril_scribe:server
```

The default listener is `127.0.0.1:8091`. Set `SCRIBE_HOST=0.0.0.0` inside a
private container network; the OCI target is
`//machine-learning/scribe/python/galadril_scribe:linux`. The service accepts
`POST /runs` only on loopback behind its Envoy sidecar. Envoy admits the Gateway's workload certificate. Frontends call Gateway.

Configure Gateway's `scribe.endpoint` as `http://127.0.0.1:8092`; its Envoy egress
listener establishes mTLS to Scribe. The reverse tool connection uses Scribe's
local egress port 8082 and the generation capability. See the
[proxy operation guide](../../docs/src/operations/proxies.md). Disable the optional chatbot
explicitly with `scribe.enabled: false` when no runtime is deployed.

On Apple Silicon, use the official [vLLM Metal plugin](https://docs.vllm.ai/projects/vllm-metal/en/stable/installation/)
in its own supported environment. On GPU servers, use vLLM's normal serving
deployment. Both share this HTTP adapter. An alternative OpenAI-compatible
server requires configuration only. Benchmark the selected model's peak memory,
prompt processing and generation throughput with PostgreSQL running before
setting concurrency; Scribe admits one generation at a time by default.

## Tools and data access

The native agent loop can run typed `search`, `graph` and `causal` tools in
parallel. Gateway supplies a short-lived generation capability outside tool
arguments and checks the actor, conversation, tenant and live Loth permissions
on every call. SQL and Cypher are fixed, parameterized adapter operations.
Ontologies retain their labels without giving the model executable queries.

Amarth analyzes its complete tenant-scoped dataset. Access to an entity's causal
summary is checked against that entity's view permission. The model receives
root statistics, never source identities, raw inputs or a complete causal chain.
Raw search and graph evidence retain their own independent permission checks.

`run_python` uses the pinned Monty interpreter in isolated worker sessions, with
memory, time, recursion and output limits. Host files, network access and host
callbacks are unavailable. This is a Python subset, not a full CPython runtime
with scientific packages installed.

For Internet or other external tools, configure `SCRIBE_MCP_URLS` as a JSON list
of trusted HTTP MCP endpoints. The native PydanticAI client manages these
sessions. Tool names are prefixed by server; Gateway capabilities are never sent
to MCP servers. External servers must own their separate credentials and be
approved destinations for the data included in their arguments. No external
MCP endpoint is enabled by default. Gateway write operations remain outside the
available tools until their action and idempotency policies are defined.

## Conversations and reconnecting

Use authenticated `ask` subscriptions on Gateway's `/graphql` WebSocket route.
Messages support owned S3 image, audio and PDF references; media support also
depends on the chosen inference model. Gateway checks the object and generates
a short-lived URL when preparing a run. PostgreSQL retains object keys.

A Gateway worker persists output independently of the subscriber. After a
disconnect, query `generationEvents(conversationId, generationId, after)` and
follow its monotonically increasing sequence, in pages of at most 64 events,
until `completed` or `failed`. A generation ID is the persisted user message ID
returned by the subscription. Replaying events does not restart inference.

Each conversation is an independent thread. Current model history is bounded
to 64 messages and 32 KiB, restricted to the actor, and reauthorized using stored
evidence. Legacy answers without provenance are excluded. Message revisions
remain immutable. Client disconnection is supported; resumption after a Gateway
process crash and transactional thread forks are separate future capabilities.

Telemetry uses metadata-only OTLP traces, metrics and structured logs. Framework
payload/exception instrumentation is disabled to keep provider bodies, prompts,
tool results, credentials and signed URLs out of exported events.

## Verification

All tests run through Bazel. Unit and real PostgreSQL tests cover delegation,
tenant isolation, revoked history, ordered events, message revisions, admission,
native tool dispatch, MCP credential isolation and sandbox resource limits.

The final stage of `//tests/e2e:pipeline_lifecycle_test` runs the real Gateway and
Scribe against the deterministic OpenAI protocol fixture. It compares chatbot
retrieval and causal statistics to the previously ingested PostgreSQL data,
tests ReBAC and Cedar ABAC, permits a root causal result with private inputs,
and verifies completion and replay after closing the WebSocket. The fixture
does not invent evidence; it combines real tool responses. This validates the
integration and security contracts, not a particular model's answer quality.
