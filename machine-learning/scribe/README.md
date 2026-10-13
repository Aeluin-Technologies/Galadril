# Galadril Scribe

Scribe runs PydanticAI agents behind Gateway's authenticated GraphQL API.
Inference is delegated to official OpenAI and/or administrator-configured vLLM servers;
Gateway owns conversations, generation events, attachments and authorization.
Scribe holds no database, SpiceDB or S3 credentials. The LaTeX report generator
and embedded mistralrs runtime have been removed.

## Running the service

Configure approved model aliases through environment variables or a deployment
secret store. vLLM is only required for aliases using that provider:

```sh
export SCRIBE_GATEWAY_TOOLS_URL='http://127.0.0.1:8082/internal/chat/tools'
export SCRIBE_MODELS='{"local":{"model":"<served-model-id>","base_url":"http://127.0.0.1:8000/v1"}}'
export SCRIBE_DEFAULT_MODEL='local'
bazel run //machine-learning/scribe/python/galadril_scribe:server
```

To combine local inference with official OpenAI, supply `SCRIBE_OPENAI_API_KEY`
from your secret store and configure both providers:

```sh
export SCRIBE_MODELS='{
  "local": {
    "provider": "vllm",
    "model": "<served-model-id>",
    "base_url": "http://127.0.0.1:8000/v1"
  },
  "cloud": {
    "provider": "openai",
    "model": "gpt-6.1-sol",
    "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    "default_reasoning_effort": "medium"
  }
}'
export SCRIBE_DEFAULT_MODEL='local'
```

With the shipped Compose stack, also set `SCRIBE_ENABLED=true` to enable
Gateway's chatbot adapter. Use `http://host.docker.internal:8000/v1` for a vLLM
server running on the Mac host; container loopback refers to the shared proxy
network, not the host. Compose forwards `SCRIBE_MODELS`, `SCRIBE_DEFAULT_MODEL`
and `SCRIBE_OPENAI_API_KEY` to Scribe. Kubernetes supplies them through the
existing `scribe-runtime` secret.

Each alias may override `api_key`; vLLM keys are separate from the shared OpenAI
key. Official OpenAI always uses `https://api.openai.com/v1/responses`, with
response storage disabled. vLLM uses the configured `/v1/chat/completions` API.
SDK environment variables such as `OPENAI_BASE_URL` cannot override these routes.
Existing aliases without `provider` remain compatible and default to `vllm`.

Remove all `vllm` aliases and select a cloud default for OpenAI-only operation.
Remove all `openai` aliases for local-only operation; no OpenAI key is required.
To run without inference, set `SCRIBE_MODELS='{}'` and unset
`SCRIBE_DEFAULT_MODEL` (or set it empty in Compose). The catalog is empty and
`POST /runs` returns 503. Neither absent provider is initialized or contacted.
Failures never switch providers automatically, preserving the selected data
destination. Set `SCRIBE_MAX_CONCURRENT_RUNS` to admit concurrent generations;
the default remains one across all providers.

The default listener is `127.0.0.1:8091`. Ambient deployments explicitly set
`SCRIBE_HOST=0.0.0.0` and `SCRIBE_PROXY_MODE=ambient`; the OCI target is
`//machine-learning/scribe/python/galadril_scribe:linux`. The service accepts
`POST /runs` and `GET /models` behind its workload proxy. Envoy admits the
Gateway's workload certificate. Frontends call Gateway.

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

## Model discovery and selection

Private `GET /models` returns the configured default and approved aliases:

```json
{
  "default_model": "cloud",
  "models": [{
    "alias": "cloud",
    "provider": "openai",
    "model": "gpt-6.1-sol",
    "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    "default_reasoning_effort": "medium"
  }]
}
```

The catalog contains configuration, not a live provider health check or the
provider's unrestricted model inventory. Credentials and backend URLs are never
included. Only models supporting function calling should be configured.
Declare the efforts actually supported by each selected model; the values are
model-dependent, as described in the [official OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
and [GPT-6.1 Sol model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
For vLLM, enable the server's tool-call parser and, where required, its reasoning
parser. An empty `reasoning_efforts` list exposes no effort selection and sends
no reasoning parameter; an omitted default leaves the provider default intact.

Authenticated clients discover the same catalog through Gateway:

```graphql
query {
  scribeModels {
    defaultModel
    models { alias provider model reasoningEfforts defaultReasoningEffort }
  }
}
```

Choose an alias and optional effort on each turn:

```graphql
subscription {
  ask(conversationId: "<id>", prompt: "Find evidence and calculate the total",
      modelAlias: "cloud", reasoningEffort: "high") {
    content
  }
}
```

Gateway forwards the choices to Scribe as `model_alias` and `reasoning_effort`.
Scribe rejects unknown aliases and unsupported efforts with 422 before reserving
capacity or calling a provider. Omitting the effort uses the alias's configured
default. The effort controls inference only; hidden reasoning is neither
streamed nor persisted. Gateway retains the existing model alias on messages.

## Tools and data access

Every provider uses the same PydanticAI agent loop, native tools, sandbox limits,
MCP toolsets and authorization checks. The native loop runs typed `search`,
`graph` and `causal` tools in
parallel. Gateway supplies a short-lived generation capability outside tool
arguments and checks the actor, conversation, tenant and live Loth permissions
on every call. SQL and Cypher are fixed, parameterized adapter operations.
Ontologies retain their labels without giving the model executable queries.

Amarth analyzes its complete tenant-scoped dataset. Access to an entity's causal
summary is checked against that entity's view permission. The model receives
root statistics, never source identities, raw inputs or a complete causal chain.
Raw search and graph evidence retain their own independent permission checks.

`run_python` provides the calculator and Python sandbox using the pinned Monty
interpreter in isolated worker sessions, with
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
HTTP protocol tests exercise official Responses and compatible Chat Completions
tool loops in isolation, together and concurrently, including real Monty
calculations, effort forwarding, secret isolation and provider failure behavior.

The final stage of `//tests/e2e:pipeline_lifecycle_test` runs the real Gateway and
Scribe against the deterministic OpenAI protocol fixture. It compares chatbot
retrieval and causal statistics to the previously ingested PostgreSQL data,
tests ReBAC and Cedar ABAC, permits a root causal result with private inputs,
and verifies completion and replay after closing the WebSocket. The fixture
does not invent evidence; it combines real tool responses. This validates the
integration and security contracts, not a particular model's answer quality.
