# Native pipeline branch routing

Branch routing belongs to the current pipeline and works without plugins.
The core catalog advertises `core.route.regex` and `core.route.slm`; pipeline
definitions use a native strategy rather than a plugin capability ID:

```yaml
name: document-routing
sources:
  - id: mail
    topic: raw
    match_pattern: '^tenant_a/raw/email/'
    group_id: email
    content_types: [text/plain]
    schema_path: schemas/avro/text.avsc
    parser: text
    source_kind: email
pipeline:
  - step: classify
    type: route
    routing: regex
    input_from: [mail]
    params:
      field: record.raw_payload.content
      pattern: 'dossier (?P<case_id>[0-9]+)'
      match_branch: legal
      default_branch: general
    branches:
      legal: [legal-processing]
      general: [general-processing]
  - step: legal-processing
    type: inference
    model: galadril_inference.models.embedding.qwen3.Qwen3EmbeddingModel
    input_from: [classify]
  - step: general-processing
    type: inference
    model: galadril_inference.models.embedding.qwen3.Qwen3EmbeddingModel
    input_from: [classify]
```

Replace the example model with a deployed built-in model or plugin capability.
The regex block preserves the input and adds `branch` plus named `extractions`.
Expressions compile once; matching runs off the event loop with a time budget.
A missing field, oversized input or timeout fails the step and follows its retry
policy. All labels must be declared, and branch destinations must cover exactly
the immediate streaming consumers. Multiple destinations on one label fork the
pipeline. Stateful joins remain unsupported.

For model-based decisions, use any alias configured in Scribe, including local
OpenAI-compatible endpoints and API providers. The block uses the same model
instances, provider settings and reasoning-effort validation as conversations:

```yaml
- step: classify
  type: route
  routing: slm
  model: local-classifier
  input_from: [mail]
  params:
    field: record.raw_payload.content
    instructions: Choose legal for litigation correspondence, general otherwise.
    fallback_models: [api-backup, another-local-model]
    timeout_seconds: 60
  branches:
    legal: [legal-processing]
    general: [general-processing]
```

`model` is a Scribe alias, not a Python class or an environment-selected provider.
Omit it to use Scribe's default. Vision obtains the private Scribe origin from
trusted `connectors.scribe_endpoint` (default `http://127.0.0.1:8092` for the
Compose egress proxy). A Kubernetes mesh bootstrap must use the Scribe Service
origin, typically `http://scribe:8091`. Provider credentials remain in Scribe.

When `fallback_models` is omitted, Scribe tries the requested/default alias, then
the current default and remaining configured models in catalog order. An explicit
list restricts the alternatives; an empty list disables fallback. Missing aliases
are skipped, including removals between attempts. At most three available models
are attempted, with one agent model call per attempt and a shared total deadline
(default 60 seconds, maximum 180). Timeout, connection failure, missing-model
responses (404/410), throttling and server errors can advance to the next model.
Authentication/authorization rejection stops the operation. A malformed decision
or an undeclared branch fails closed; it never creates a destination.

The private `POST /pipeline/route` endpoint is restricted to Vision's workload
identity. It cannot access tools, conversation history, external actions or a
user's delegated account. Scribe's concurrency budget also applies. Responses
contain the chosen branch, actual model alias and whether fallback occurred.
Vision persists this provenance with the step result; completed replays use
the stored branch and do not call a model again. Regression tests use simulated
local/API models and never send live provider requests.

## Content and metadata

Dotted paths address the actual incoming payload: standard normalized metadata
includes `record.metadata.language`, `input_type`, `mime_type`, and `source_kind`;
source-specific fields remain under `record.raw_payload`. The title and date paths
below are examples for records enriched with those fields by a source or upstream
block. Routing does not invent or populate missing business metadata.

Both regex and Scribe routing read `record.raw_payload.content` by default, the
text body produced by Intake's text parser. Binary documents need a text
extraction or OCR block before text routing. Set `params.field` to another dotted
payload path to inspect a title or another textual field instead.

Scribe routing can receive an explicit whitelist of additional metadata:

```yaml
params:
  field: record.raw_payload.content
  metadata_fields: [record.metadata.title, record.metadata.date]
  instructions: Choose legal for an appeal, general otherwise.
```

Only these fields accompany the text, as a bounded JSON object with `content`
and `metadata`. Missing optional metadata becomes `null`. Credentials and other
payload fields are not automatically sent to the model.

Use `core.route.metadata` for deterministic key/value selection without a model:

```yaml
- step: select-dated-appeal
  type: route
  routing: metadata
  input_from: [mail]
  params:
    conditions:
      record.metadata.title: Administrative appeal
      record.metadata.date: '2026-10-23'
    match_branch: legal
    default_branch: general
  branches:
    legal: [legal-processing]
    general: [general-processing]
```

All expected fields must match their scalar values and types exactly. Missing
fields select the default branch; `1`, `true`, and `'1'` are different values.
Dates are compared as supplied strings, without timezone or date coercion.

## Transfers to another pipeline

A terminal native transfer selects a published tenant-local pipeline, an immutable
Registry revision, and one receiving input. For example, a router's `legal` branch
can point to this block:

```yaml
- step: legal-processing
  type: pipeline
  input_from: [classify]
  pipeline_ref:
    pipeline_id: law
    revision_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    input_source: mail
```

The example revision must be replaced with the target's actual published Registry
revision. New drafts and publications must select the current published revision.
Already published parent revisions retain their original pins when the child is
updated. There is no implicit return: a transfer cannot have downstream blocks in
the parent. Add any common processing before the transfer.

The Gateway filters selectable targets by `View` and `Execute`. Reading a linked
revision requires `View`, and creation, update, and validation recheck both rights
recursively before loading dependencies. Publishing also requires `Publish` on
each linked pipeline, preventing authors from granting access to another graph
through a parent publication without delegation authority. Tenant identity comes
from the authenticated request, never from the reference. Permission failures use
GraphQL `FORBIDDEN`; dependency failures do not expose raw storage errors.

The Registry compiler expands references into the root execution graph before
Vision starts. Only the selected input's event-driven descendants are attached;
other inputs and independent scheduled blocks do not start. The payload passes
through unchanged, so compatible input contracts are the author's responsibility.
A transfer neither rereads IMAP nor creates another ingestion job. The original
execution identity and durable branch results remain in use. Compiler-owned
provenance resolves each child block's ontology against the child's pinned
revision; payloads and authored definitions cannot forge that provenance.

Admission limits apply to the full composition:

- At most **three cross-pipeline transfer occurrences**, including nested and
  repeated references. Ordinary local router branches do not consume this budget.
- At most **256 source and processing blocks** after inclusion. Each occurrence
  of a referenced definition is charged, including its source contracts and
  independent input blocks, so selecting an input cannot hide an oversized graph.
- Cycles are rejected by pipeline identity even when a cycle uses different
  revision IDs. Stateful joins and continuations after transfers are rejected.

The dashboard offers native regex, Scribe, and metadata routing, labeled branch
connections, and a permission-filtered pipeline picker with receiving input
selection. Linked contents expand read-only and are reread under current access
rights on each expansion. Server validation supplies the authoritative expanded
block count before YAML export. The exported definition preserves references;
internal forwarding blocks and provenance are never authored by the browser.
