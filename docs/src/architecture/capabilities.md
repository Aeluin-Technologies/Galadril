# Capability catalog contract

A capability is deployment metadata, not execution authority. Version 1 uses stable
namespaced IDs, an origin (`plugin` or `user`), optional plugin identity, a kind
(`input`, `output`, `model`, `mcp`, `skill`, `algorithm`), execution targets (`pipeline`, `scribe`), and an object JSON Schema
for parameters. Optional step type and model class identify existing pipeline
bindings. Descriptors never contain endpoints, credentials, or executable source.
Catalogs are bounded, reject duplicate IDs and invalid namespaces, and fail closed
on unknown contract versions. Built-in pipeline descriptors have one JSON source
shared by Rust Gateway and Python. Execution status distinguishes `registered` bindings from `declared` metadata.
It does not guarantee provisioned weights, reachable services, or permission to execute.

Gateway exposes authenticated `pipelineCapabilities` and `scribeCapabilities`
queries with kind and plugin filters. Active tenant membership is checked before
catalog access. Pipeline discovery reads the packaged runtime inventory through
Scribe's private `/pipeline-capabilities` route; Scribe tool discovery uses
`/capabilities`. Both use bounded transport. A runtime
failure is reported, never converted into an empty successful catalog. A disabled
Scribe has an empty catalog. Model alias selection remains `scribeModels`.

Scribe accepts trusted injected tool extensions, each with an MCP or algorithm
descriptor and a toolset factory. IDs are unique and tool names are prefixed by
the full capability ID. Run requests explicitly select extension IDs; omitted
selection exposes no injected extension. Unknown or duplicate IDs fail before
admission and before invoking factories. Toolsets are created per run and use
native PydanticAI lifecycle, timeout, tracing and tool-call budgets. MCP factories
receive no Gateway bearer token. Native tools and legacy administrator-configured
MCP URLs retain their current behavior. Public metadata never exposes MCP URLs.

The [official plugin runtime](official_plugins.md) now discovers packaged manifests,
loads only reviewed factory paths, and binds pipeline `capability_id` selections.
This boundary does not implement plugin downloads, tenant installation,
tenant-selected imports, arbitrary new step kinds, or approval workflows. Trusted algorithm
toolsets can use request dependencies, and must preserve Gateway authorization for
private data and explicit approval for side effects. Catalog discovery grants no
permission and no approval. Source selectors and microservice lifecycles are documented in the official runtime
contract and `plugins/README.md`. Native branch routing is advertised by the core
catalog and documented in [Native pipeline branch routing](pipeline_routing.md).

## GraphQL discovery

```graphql
query AvailableBlocks {
  pipelineCapabilities(kind: INPUT, pluginId: "core") {
    schemaVersion
    capabilities {
      id
      pluginId
      pluginVersion
      origin
      execution
      compatibility { allowedModels requires }
      name
      kind
      targets
      stepType
      modelClass
      parametersSchemaJson
    }
  }
  scribeCapabilities(kind: MCP) {
    schemaVersion
    capabilities { id pluginId name kind targets parametersSchemaJson }
  }
}
```

Omit `kind` to list all categories. An unknown plugin filter returns an empty
list. Expired authentication and inactive membership produce `FORBIDDEN` with
the queried field's GraphQL error path. Private Scribe failures produce
`CAPABILITY_CATALOG_UNAVAILABLE`. The JSON Schema is returned as a JSON string
for compatibility with existing Gateway conventions. The native pipeline manifest
currently describes Kafka ingress, transactional ESKG output, resolution, causal
analysis, and the thirteen packaged inference models. dbt is not advertised because
Vision has no default dbt execution adapter.

The private Scribe API exposes `GET /capabilities`. `Runtime(...,
extensions=(ToolExtension(descriptor, toolset_factory), ...))` composes reviewed
code, and `tool_ids` in `/runs` or `/generations` selects up to 32 extension IDs.
A tool from `law.algorithm.timeline` named `build` is exposed as
`law_algorithm_timeline_build`. Names that normalize to the same prefix are
rejected. Extension tool calls have a 30-second deadline and metadata-only OTLP
spans; the model never selects factory code or infrastructure credentials.
Native web/library enable flags and configured model aliases are unchanged.

## Documentation audit: MCP and Agent Skills

Verified against the official documentation on 2026-10-17:

- [PydanticAI MCP client](https://pydantic.dev/docs/ai/mcp/client/) and
  [toolset approval wrappers](https://pydantic.dev/docs/ai/tools-toolsets/toolsets/#requiring-tool-approval).
- [OpenAI MCP guidance](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
  and [Agent Skills](https://developers.openai.com/plugins/concepts/skills).
- [PydanticAI Skills](https://pydantic.dev/docs/ai/harness/skills/).

Scribe uses client-side `MCPToolset` instances through PydanticAI's native agent
loop, lifecycle, prefixing and tool budgets. OpenAI receives ordinary function
calls in this mode; its provider-hosted MCP `require_approval` setting is not
implicitly applied. Galadril must enforce its own delegated authorization and
bridge human approvals to PydanticAI's deferred-tool mechanism. A reviewed plugin
can supply `requires_approval=True` tools or an `approval_required()` toolset.
Regression tests verify both forms across the extension wrappers: the call is
paused before its body, a native denial executes nothing, and a native approval
executes it once. Scribe's current streaming API cannot resume approvals; a
protected call fails closed rather than silently executing. End-to-end approval
persistence and resumption remain unimplemented. Selecting a capability ID is not
an approval of any tool call or its arguments. Legacy configured MCP services are
not automatically assigned an approval policy by this change.

Agent Skills are a separate instruction/workflow layer, not an MCP endpoint or
an algorithm tool. A portable package contains `SKILL.md` with `name` and
`description` frontmatter, workflow instructions, and optional supporting files.
Discovery should expose the selected metadata first and load full instructions
only on demand. Skill instructions grant no tools, credentials, filesystem access,
or authority to perform actions. Both plugins and users may supply skill libraries through future scoped registration;
only explicitly selected libraries should be exposed; skill frontmatter must not override Gateway policy.

The catalog includes the `skill` category. Python-defined workflows now have a
native deferred-capability binding through `WorkflowSkill`, described below.
There is no portable `SKILL.md` package loader or resource sandbox integration.
Unbound skill definitions must use `execution: declared`; they cannot execute.
The workspace pins `pydantic-ai-slim==2.57.0` and
`pydantic-ai-harness[skills]==0.57.0`. The migration retains native structured
outputs, validators, MCP toolsets, capabilities and model reasoning settings.
The unused Instructor dependency was removed to resolve its incompatible OpenAI
SDK constraint; OpenAI is now 3.33.0. Loading portable Skills still requires a
bounded workspace exposing only reviewed plugin resources. Do not substitute
arbitrary filesystem scanning or unrestricted script execution for that boundary.
The existing Monty sandbox does not provide a general skill filesystem or shell.

## User contributions and compatibility

Plugin and user contributions share the same contract. Plugin contributions carry
`plugin_id`, `plugin_version`, and IDs in the plugin namespace. User contributions
use the reserved `user.*` namespace, `origin: user`, and null plugin identity.
The namespace identifies provenance; it does not identify a principal or authorize
access. The server must derive ownership and tenant/project scope from trusted
registration and authorization state, never from a submitted descriptor.

`compatibility.allowed_models` is an optional allowlist of exact runtime model
identifiers: Scribe model aliases, or model class identifiers for pipeline
composition. Empty means no declared model restriction. `compatibility.requires`
is a bounded list of other capability IDs that must be explicitly selected in
the same execution context. Dependencies are not automatically enabled, executed,
or granted privileges. The validator checks every selected capability, so each
dependency's own constraints also apply. These are co-selection constraints,
not an execution-order graph. Self-dependencies, duplicates, and oversized
constraints are rejected.

`CapabilityCatalog.validate_selection(ids, target=..., model=...)` rejects unknown
and duplicate IDs, missing dependencies, unsupported targets, unbound declarations,
and incompatible models. Scribe invokes this validation before reserving a run
or generation and again before creating per-run toolsets. Plugin and user MCPs
and algorithms use the same trusted native `ToolExtension` factory interface.
User provenance does not permit importing Python source from a request.

Scribe's composition root also accepts `declared_capabilities` for listable,
unbound definitions, including Skills. GraphQL exposes `origin`, nullable
`pluginId`/`pluginVersion`, `execution`, and typed `compatibility` fields through
both existing discovery queries. Omit the plugin filter to include user items.
Existing manifests default to plugin origin, registered bindings, and unrestricted
compatibility.

Composition injection is not a tenant installation store. A runtime containing
user contributions must be confined to their authorized tenant/project scope;
do not install such contributions in a runtime shared across unrelated tenants.
The current production composition registers no user contributions. Future user
registration endpoints and tenant/project catalog resolution must enforce that
scope before discovery or execution. Public pipeline configurations now select
official capability IDs through the shared registry; user contributions still
require separately scoped installation and authorization contracts.
Portable `SKILL.md` loading and the approval resumption API remain separate work.

## Agent teams

A team is a trusted, immutable composition of one to eight named agents. Every
agent has a bounded mission and optional Skill IDs. `team_id` selects a registered
team in the private Scribe run/generation request; a request cannot supply agent
code, tool URLs, credentials, or extra permissions. The current model alias and
selected tool IDs apply to the entire team. Unknown teams, missing Skills, and
incompatible Skill/model selections fail before admission or factory invocation.
Team registrations follow the same tenant/project scope rules as tool extensions.

Scribe uses PydanticAI's programmatic multi-agent pattern. Workers execute in
configured order with isolated message histories and the same authorized tools
and generation-scoped dependencies. They receive the current request and prepared
context, not the previous conversation or another worker's report. The final
orchestrator retains conversation history and receives bounded, attributed reports
as untrusted data. Only its response is streamed to the user. Failure of a worker
fails the team instead of silently synthesizing an incomplete result. One timeout,
request budget, tool-call budget, and web-search budget cover the whole execution.
Sequential execution avoids concurrent checks overspending the shared budget.

`WorkflowSkill` binds a Skill descriptor to bounded Python-defined workflow
instructions. It uses PydanticAI's native deferred `Capability`: metadata is
visible first and the model calls `load_capability` to activate the instructions.
Each worker receives only its assigned Skills; these workflows provide no tools,
filesystem access, shell, or extra authority. Factories and MCP sessions remain
per agent run. The existing portable `SKILL.md`/Harness loader remains separate;
this workflow binding does not parse skill packages. Skill IDs and their model
restrictions are exposed in the existing capability catalog. The private
`GET /teams` endpoint lists team definitions without workflow instruction bodies.

This is request-driven execution. Recurring watches, scheduling, independent
agent memory, public Gateway selection, and user registration UI are not part of
this boundary. Selecting a team does not schedule future work.

References: [PydanticAI multi-agent applications](https://pydantic.dev/docs/ai/guides/multi-agent-applications/)
and [native deferred capabilities](https://pydantic.dev/docs/ai/capabilities/overview/).

### Native PydanticAI implementations

`AgentDefinition.implementation_id` may select a trusted server-registered native
agent binding. `NativeAgentBinding(agent)` accepts PydanticAI `AbstractAgent`
implementations directly; it does not rebuild their output schemas, validators,
retries, hooks, or capabilities. Structured Pydantic results become JSON reports
for synthesis. Other native output types can use an explicit typed report
serializer. Result validation and retries still happen inside PydanticAI before
serialization. The orchestrator receives each report with its agent ID.

```python
from pydantic import BaseModel
from pydantic_ai import Agent
from galadril_scribe.agents import NativeAgentBinding
from galadril_scribe.runtime import Dependencies
from galadril_scribe.teams import AgentDefinition, TeamDefinition


class EmailFacts(BaseModel):
    sender: str
    subject: str
    deadlines: list[str]


email_agent = Agent(deps_type=Dependencies, output_type=EmailFacts)
email_binding = NativeAgentBinding(email_agent)
email_team = TeamDefinition(
    id="utils.team.email",
    name="Email analysis",
    agents=(
        AgentDefinition(
            id="extract",
            mission="Extract only facts supported by the supplied email.",
            implementation_id="utils.agent.email",
        ),
    ),
)
# Pass teams=(email_team,) and
# agent_bindings={"utils.agent.email": email_binding} to the scoped Runtime.
```

Plugins retain the native agent object for synthetic tests and Pydantic Evals;
Galadril does not introduce a competing evaluation engine. Pydantic Evals and a
persistent evaluation service are not installed by this change. Native agent
hooks and capabilities remain available to plugin authors. The run-supplied
model, dependencies, shared usage limits, selected tools, and assigned workflow
Skills are supplied by Galadril. Native bindings are reviewed Python deployment
code, not executable definitions accepted from an HTTP request. Their own tools
and capabilities must honor delegated authorization and registration policy, just
as existing tool factories must. The supplied serializer must not include secrets
or privileged state in the report.

Each worker emits a metadata-only OpenTelemetry span with team/agent IDs, status,
model-request count and tool-call count. These spans flow through Scribe's existing
OTLP exporter configuration; prompts, report bodies, credentials, and exception
messages are excluded. Native instrumentation is retained for custom agents, so
plugins opting into additional PydanticAI instrumentation must configure content
redaction and their own hooks appropriately. The built-in agent keeps automatic
framework instrumentation disabled because exception payloads may contain data.
Regression tests cover schemas and validators, shared tools and budgets, lazy
workflow loading, isolation, deadlines, bounded reports and telemetry redaction.

## Interactive runs

The default agent and each team member receive native Harness Planning and
input, tool and output guardrails. Background generations additionally receive
native AskUser, whose answerer waits up to 120 seconds for a generation-scoped
response. A question is information gathering, never approval for a protected
MCP or algorithm call. The overall run deadline and shared usage budgets still
apply while waiting for an answer.

Scribe emits bounded, typed public activity payloads alongside content and
legacy tool events. Activities describe phases, plan changes, questions and their
resolution; they never include private chain of thought, tool arguments, provider
responses or credentials. Each agent may alternate writing, planning and tools
multiple times. Text is released after a complete model text block has passed
credential redaction, rather than exposing unchecked token fragments that might
split a secret across deltas. Native output guardrails alone do not protect
streamed deltas, so this presentation boundary is enforced separately.

Gateway persists activities with the existing ordered generation log, including
its temporary-table variant. GraphQL `answerPengoloQuestion` checks current tenant,
conversation and project access, generation ownership and active status before
forwarding an answer with the private delegated capability. Scribe independently
checks that capability, the pending question and its exact headers/options;
identical retries are idempotent and conflicting retries fail. The browser never
receives the delegated capability. Answer bodies are supplied to the running
native agent, not copied into telemetry.

The dashboard renders animated task status changes, bounded question forms and
continued tool/text activity. Readers may observe a shared generation, while only
its initiator may answer. Reduced-motion preferences disable activity animations.
Completed activity remains visible for the current cached conversation run.

The default policy redacts configured provider/delegation credentials and native
secret patterns, and blocks sensitive structured tool values. This is a baseline
credential policy, not a general classifier for personal or confidential data.
Plugins handling sensitive domains must add appropriate native policies while
preserving Gateway authorization. Progress uses the existing OTLP pipeline and
records metadata only.

Persistent memory, scheduled watches, shell execution, X Search, Advisor,
Trajectory Judge and a persistent evaluation service are not enabled by default.
Trusted native agent bindings can compose upstream capabilities subject to their
own authorization and resource policy; installing the SDK does not grant these
abilities to user-submitted definitions. Public team selection and contribution
registration endpoints remain future work.

References: [Harness guardrails](https://pydantic.dev/docs/ai/harness/guardrails/),
[Planning](https://pydantic.dev/docs/ai/harness/planning/) and
[Ask User](https://pydantic.dev/docs/ai/harness/ask-user/).
