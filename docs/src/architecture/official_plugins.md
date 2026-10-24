# Official plugin runtime contract

Official plugins are trusted, reviewed deployment code packaged from `plugins/`.
Tenant pipeline definitions select capability IDs, never Python imports, service
commands, credentials, or infrastructure endpoints. Discovery grants no authority
to act on an external account. Third-party installation is outside this contract.

At startup an immutable registry discovers packaged manifests, checks ownership,
versions, dependencies and execution bindings, and rejects inconsistent bundles.
Pipeline bindings create one handler per published tenant/pipeline/revision/step.
Models and algorithms use the same execution port as built-in steps. Scribe
adapts toolset factories and packaged Skills to its existing approval and
capability-selection contracts. Public discovery contains metadata only.

`algorithm` steps select `capability_id`. Inference may select a model capability
instead of a built-in model name. Native `route` steps select `routing: regex` or
`routing: slm` without `capability_id` and declare `branches`, mapping labels
to immediate downstream step IDs. A handler returns `branch` alongside its
payload. Only that branch advances. Unknown labels, empty branch maps, undeclared
edges and destinations outside the immediate DAG are errors. The durable step
result stores the decision; replays never ask a model to decide again.

Ingestion remains tenant-bound. Source `group_id` and `content_types` are optional
conjunctive filters applied to the storage event, in addition to `match_pattern`.
They are independent of `source_kind` and MIME-derived modality. A mail connector
writes `tenant/raw/email/...txt`; an email source can select group `email`, parser
`text`, and source kind `email`, while the payload remains text. Different
published pipelines may consume the same source. Ambiguous sources within one
publication remain errors. Project identity is not inferred from event metadata.

A plugin service is an independently deployed worker selected from its manifest,
with a bounded cooperative shutdown lifecycle. Vision and Scribe never start arbitrary
processes or receive a Docker socket. The deployment supplies account credentials,
tenant, input group, storage permissions and OTLP settings. An IMAP worker must
checkpoint only after confirmed ingestion, bound message size and poll batches,
and use stable object identities for retries. Services are disabled unless
explicitly deployed; discovering a plugin must not read or send emails.

Regex routing compiles expressions once and bounds evaluation time and input size.
SLM routing uses Scribe model aliases, structured labels, bounded fallback and a
shared deadline. See [Native pipeline branch routing](pipeline_routing.md). Model output cannot add graph edges or authorize external actions.
All extension calls participate in existing OTLP traces; telemetry contains IDs,
outcomes and duration, never message bodies, tool arguments or account secrets.


The editor reads the authenticated live capability catalog and pins selected
plugin versions in sources and steps. Gateway validates parameters and deployed
versions on composition, creation, update and publication; unavailable versions
fail closed. JSON Schema validation uses normalized source defaults consistently
before and after Vision loads a published revision. Ordinary parameter schemas
apply to plugin parameters, excluding engine-owned scheduling/retry fields.

Scribe model factories return native PydanticAI models with provider metadata.
Their capability IDs become Scribe aliases, selectable in conversations,
document extraction and native SLM routes. A removed alias follows the route's
configured fallback policy. Model capabilities are distinct from selectable
tools and Skills.

The plugin package provides an authorized ingestion client and an explicit
worker deployment renderer for Kubernetes and Compose. Ingestion obtains a
scoped staging upload and finalizes through Gateway before returning a receipt.
Shared HTTP credentials never accompany a presigned storage upload. Delivery has
bounded payload/response sizes and a total deadline; it does not automatically
retry mutations with an uncertain outcome. Image publication includes an amd64
and arm64 plugin worker, while services remain opt-in. `utils`, `law` business
features and IMAP are deferred.
