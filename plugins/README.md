# Official plugins

This package is deployment code. `PluginRegistry.discover()` scans packaged
`galadril_plugins/<id>/plugin.json` resources at startup and validates the complete
inventory before exposing execution. It never fetches repositories or starts an
external-account integration during discovery. `utils` and its IMAP connector are
not implemented here.

Each manifest supplies `schema_version: 1`, `id`, `version`, optional exact-version
plugin `requires`, public `capabilities`, and an exact `bindings` map. A binding
contains a `pipeline` factory, a `scribe` toolset factory, a native Scribe `model` factory, a packaged `skill`
Markdown path, or an input `service` name. The module namespace must be
`galadril_plugins.<id>`. New directories are included automatically in the Bazel
library and Python wheel. Gateway discovers the pipeline catalog through Scribe's
private, authenticated network route. Deploy the same plugin bundle with all
three runtimes.

A pipeline factory receives `(PluginScope, PipelineStep)` and returns an object
with `async execute(command, step) -> dict[str, JsonValue]`. The scope comes from
trusted tenant/pipeline/revision composition, never command attributes. Select
model, algorithm and output implementations through `capability_id`.
An inference model capability replaces the built-in `model` lookup entirely.

Scribe factories return native PydanticAI toolsets. Packaged Skills become native
workflow Skills. Existing explicit tool selection, model compatibility, human
approval and delegated Gateway authorization remain in force. Discovery adds no
external-action permission.

Branch routing is native pipeline control, not a plugin contribution. See
[Native pipeline branch routing](../docs/src/architecture/pipeline_routing.md) for
regex, Scribe-backed decisions, model fallback and complete YAML examples.

A future plugin input declares a public `input` capability with `step_type:
source`, a binding such as `{"service":"listener"}`, and a `services` entry
such as `{"listener":"galadril_plugins.example.listener.Service"}`. Its factory
receives deployment environment and returns an object implementing
`async run(stop: asyncio.Event) -> None`. The service owns its connections and
must release them in `finally`, including on cancellation or failure.

Run a single declared microservice explicitly:

```sh
bazel run //plugins:service -- example listener
# Installed Python package:
uv run galadril-plugin-service example listener
```

SIGINT/SIGTERM set the stop event; service failures exit the worker so its
orchestrator can restart it. The worker exports OTLP traces, metrics and logs.
Use the normal authorized Gateway upload/finalization flow to ingest documents;
direct S3 writes do not supply the scoped ingestion delegation required by Intake.
Give the service an account restricted to the intended tenant and input group.
Service deployment and credential provisioning are operator responsibilities;
Vision never spawns arbitrary programs or receives a container-management socket.

## Authoring and compatibility

The pipeline editor's **Add a plugin block** action reads Gateway's authenticated
catalog. It offers installed pipeline input, model, algorithm and output
capabilities, initializes declared parameter defaults, and keeps
`capability_id` / `capability_version` in YAML exports. Parameters with scalar
properties have form controls; complex structures use the advanced JSON editor.
Gateway revalidates the complete composed definition against the live deployed
inventory before creating, updating or publishing it. Missing capabilities,
changed versions, unmet dependencies and invalid JSON Schema parameters reject
the operation. Metadata discovery imports no executable factories.

A model contribution targeting Scribe uses a `model` binding whose factory takes
its shared `httpx.AsyncClient` and returns
`galadril_scribe.plugins.RegisteredModel(model, config)`. `model` is a native
PydanticAI Model; `config` is a Scribe `ModelConfig` describing its provider and
public model name. Its capability ID becomes its unique Scribe model alias.
Aliases cannot collide with configured models. Such models are available to
conversations, document extraction and native SLM branch routing, including its
existing fallback policies. They are model choices, never tool selections.
A capability targeting both pipeline and Scribe supplies both factories.

Schemas use JSON Schema Draft 2020-12 and packaged local references only.
Non-source schemas validate plugin `params`; engine-owned trigger, cron and retry
settings are excluded. Input schemas validate normalized source defaults and its native topic,
selector, parser, source kind, group and content-type fields, excluding its ID
and capability reference. Connection endpoints, account credentials and process
settings belong to the worker deployment, never this source configuration.

The native compiler still enforces three transfers to other pipelines and 256
blocks in the expanded composition. It rechecks access to linked revisions and
rejects cycles. A plugin block in a linked pipeline receives that block's trusted
origin pipeline/revision while commands stay bound to the root execution.

## Authorized input delivery

`galadril_plugins.ingestion.IngestionClient` provides the upload protocol for a
future connector. Construct it once with deployment-owned `IngestionSettings`, a
shared HTTP client and an async token provider. The provider is called for each
Gateway mutation to permit credential rotation. Supply the authenticated public
Gateway `/graphql` URL, the exact tenant/account subject, destination `group_id`
and an allowlist of presigned storage origins.

`await client.upload(content, name="message.txt", content_type="text/plain")`
first obtains an owner-scoped staging key, uploads bytes without forwarding
Gateway credentials or client cookies, and finalizes through Gateway. It returns
an `IngestionReceipt` only after finalization has established the ingestion
delegation. There are no automatic mutation retries. Advance the source's durable
checkpoint after the receipt and tolerate redelivery after an uncertain failure.
Use portable filenames; preserve business titles and dates inside the document's
metadata. The payload size, total delivery time and response sizes are bounded.
Select a dedicated ingestion pipeline with a source group and `source_kind`;
MIME alone does not distinguish email from other text.

## Deploying a worker

`bazel build //plugins:images` builds amd64 and arm64 worker images. The regular
image publication target includes `//plugins:push`. Build the same reviewed plugin
bundle into Vision, Scribe and the worker; test fixtures under `plugins/tests/`
are excluded from those images and from the wheel.

Render one Kubernetes deployment without starting it:

```sh
bazel run //plugins:deployment -- example listener \
  --image registry.example/galadril-plugins@sha256:<digest> \
  --secret plugin-example --namespace galadril
```

The image must be pinned by digest. The referenced Secret supplies the plugin's
own account configuration and OTLP exporter settings. The deployment runs as a
non-root user with a read-only root, bounded resources, no service-account token,
and a 30-second termination grace period. The worker gives cooperative shutdown
20 seconds, then cancels the service. It uses `Recreate` to avoid overlapping
listeners during a rollout; connectors must still implement durable source
checkpoints and idempotent delivery. Replication and leader election remain
connector responsibilities.

Reach Gateway through its authenticated public ingress. This deployment does not
receive Scribe's private API or a bypass of Gateway authorization. Configure
outbound access to the intended external service, storage and OTLP collector in
the deployment's network policy. Provision narrowly scoped accounts and rotate
tokens through the supplied token provider. No connector is started by catalog
discovery, and no container-management socket is exposed.

`//machine-learning/scribe/tests:packaged_plugins_test` stages a test-only reviewed
bundle into a fresh process and exercises actual startup discovery, independent
service loading, a scoped pipeline handler, packaged Skill/tool/model discovery
and native Scribe routing without paid model calls.

To render an opt-in Compose service, use `--format compose` with
`--environment-file ./.local/plugin.env` instead of `--secret`. Keep the environment
file outside version control. Start the generated file with:

```sh
docker compose -f plugin.json --profile plugins up -d
```

Add the required external network explicitly when the worker uses a private
deployment network. The generator never reads the environment file or starts a
service.
